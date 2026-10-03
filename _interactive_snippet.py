async def _run_interactive(config: dict, args):
    from .models import WorkspaceDeps, UndoManager
    from .agent import create_agent
    from .session import SessionManager
    from .context_compressor import TokenEstimator

    workspace_dir = config["workspace_dir"].resolve()
    workspace_dir.mkdir(parents=True, exist_ok=True)
    if not workspace_dir.is_dir():
        console.print(f"[red]错误: 工作目录路径存在但不是目录: {workspace_dir}[/red]")
        sys.exit(1)

    config, project_config, perms, skills_mgr, subagents_mgr, mcp_toolsets = (
        _build_managers(config)
    )

    if args.model:
        config["model"] = args.model
    perms.headless = False
    if args.solo:
        perms.solo_mode = True

    console.print("FoxCode")
    console.print(f"[dim]工作目录: {workspace_dir} | 模型: {config['model']}[/dim]")
    if project_config["instructions"]:
        console.print(
            f"[dim]项目指南: .foxcode/instructions.md 已加载 ({len(project_config['instructions'])} 字符)[/dim]"
        )
    if project_config["rules"]:
        console.print(
            f"[dim]用户规则: .foxcode/Rules.md 已加载 ({len(project_config['rules'])} 字符, AI 只读)[/dim]"
        )
    if project_config["memory"]:
        console.print(
            f"[dim]项目记忆: .foxcode/Memory.md 已加载 ({len(project_config['memory'])} 字符)[/dim]"
        )
    if mcp_toolsets:
        console.print(f"[dim]MCP 服务器: {len(mcp_toolsets)} 个已配置[/dim]")
    console.print()

    # 展示未提交的 git 变更 (弃用，请勿删除)
    # _show_git_status_hint(workspace_dir)

    proxy_mounts = _build_proxy_mounts(config)

    async with RetryClient(
        mounts=proxy_mounts or None,
        timeout=httpx.Timeout(config["request_timeout"]),
    ) as http_client:
        undo_manager = UndoManager()
        session_manager = SessionManager(workspace_dir / ".foxcode" / "sessions")
        deps = WorkspaceDeps(
            workspace_dir=workspace_dir,
            http_client=http_client,
            undo_manager=undo_manager,
            console=console,
            shell_timeout=config["shell_timeout"],
            project_instructions=project_config["instructions"],
            permissions=perms,
            plan_mode=False,
            skills=skills_mgr,
            subagents=subagents_mgr,
            mcp_toolsets=mcp_toolsets,
            config=config,
        )

        skills_list, subagent_list = _agent_lists(skills_mgr, subagents_mgr)
        agent = create_agent(
            config,
            http_client,
            project_config["instructions"],
            mcp_toolsets=mcp_toolsets,
            skills_list=skills_list,
            subagent_list=subagent_list,
            rules=project_config["rules"],
            memory=project_config["memory"],
        )

        all_messages = []
        max_history_messages = 50
        max_context_tokens = config.get("max_context_tokens", 100000)
        token_estimator = TokenEstimator()

        terminal_mode = False
        terminal_cwd = workspace_dir
        pending_skill = None

        # 初始化 prompt_toolkit session
        history_file = workspace_dir / ".foxcode" / "history"
        pt_session = _make_prompt_session(history_file)

        # 自动加载默认命令文件
        # 若文件以 [Command] 开头：其后每一行作为命令在启动时逐条执行
        # 否则整个文件内容作为默认提示发送给 AI
        default_prompt, startup_commands = _parse_foxcode_md(workspace_dir)
        if startup_commands:
            console.print(
                f"[dim]已加载默认命令: .foxcode/foxcode.md "
                f"({len(startup_commands)} 条启动命令)[/dim]"
            )
        elif default_prompt:
            console.print(
                f"[dim]已加载默认提示: .foxcode/foxcode.md "
                f"({len(default_prompt)} 字符)[/dim]"
            )

        async def _run_loop():
            nonlocal \
                all_messages, \
                terminal_mode, \
                terminal_cwd, \
                pending_skill, \
                default_prompt, \
                agent
            while True:
                try:
                    if startup_commands:
                        prompt = startup_commands.pop(0)
                        console.print(Text(f">> {prompt}", style="dim"))
                    elif default_prompt is not None:
                        prompt = default_prompt
                        default_prompt = None
                    else:
                        if terminal_mode:
                            prompt_text = f"{terminal_cwd}> "
                        else:
                            prompt_text = ">> "
                        prompt = (await pt_session.prompt_async(prompt_text)).strip()
                except (EOFError, KeyboardInterrupt):
                    break

                # Ctrl+X detection: toggle terminal mode
                if "\x18" in prompt:
                    prompt = prompt.replace("\x18", "").strip()
                    terminal_mode = not terminal_mode
                    status_text = "开启" if terminal_mode else "关闭"
                    console.print(
                        f"[yellow]终端模式 {status_text} (Ctrl+X 切换)[/yellow]"
                    )
                    if not prompt:
                        continue

                if not prompt:
                    continue

                if terminal_mode:
                    cmd_text = prompt.strip()
                    if cmd_text == "cd":
                        console.print(str(terminal_cwd))
                        continue
                    elif cmd_text.startswith("cd "):
                        target = cmd_text[3:].strip().strip('"').strip("'")
                        try:
                            new_cwd = Path(target)
                            if not new_cwd.is_absolute():
                                new_cwd = (terminal_cwd / new_cwd).resolve()
                            else:
                                new_cwd = new_cwd.resolve()
                            if new_cwd.is_dir():
                                terminal_cwd = new_cwd
                            else:
                                console.print(f"cd: {target}: 没有那个目录")
                        except (OSError, ValueError):
                            console.print(f"cd: {target}: 没有那个目录")
                        continue

                    console.print(Text(f"  执行: {prompt}", style="dim"))
                    try:
                        subprocess.run(
                            prompt,
                            shell=True,
                            cwd=str(terminal_cwd),
                        )
                    except KeyboardInterrupt:
                        console.print("\n[yellow]命令已中断[/yellow]")
                    except Exception as e:
                        console.print(f"[red]错误: 命令执行失败 - {e}[/red]")
                    continue

                if prompt.startswith("/"):
                    cmd = prompt.strip().lower()
                    if cmd in ("/exit", "/quit"):
                        _save_session(session_manager, all_messages)
                        return
                    elif cmd == "/help":
                        print_help()
                        continue
                    elif cmd.startswith("/goal"):
                        goal_text = (
                            prompt.split(maxsplit=1)[1]
                            if len(prompt.split(maxsplit=1)) > 1
                            else ""
                        )
                        if not goal_text:
                            goal_text = console.input(
                                "[bold cyan]请输入目标: [/bold cyan]"
                            ).strip()
                        if not goal_text:
                            console.print("[yellow]已取消（目标为空）[/yellow]")
                            continue
                        try:
                            all_messages = await _run_goal_loop(
                                agent, goal_text, all_messages, deps, config
                            )
                        except Exception as e:
                            _print_run_error(e)
                            console.print(
                                "  [yellow]目标执行因错误中断，已保留当前进度，"
                                "可稍后再次执行 /goal 继续[/yellow]"
                            )
                        continue
                    elif cmd == "/plan":
                        deps.plan_mode = not deps.plan_mode
                        perms.plan_mode = deps.plan_mode
                        console.print(
                            f"[yellow]计划模式 {'开启' if deps.plan_mode else '关闭'}[/yellow]"
                        )
                        continue
                    elif cmd == "/cot":
                        deps.cot_mode = not deps.cot_mode
                        console.print(
                            f"[yellow]CoT 思维链模式 {'开启' if deps.cot_mode else '关闭'}[/yellow]"
                        )
                        if deps.cot_mode:
                            console.print(
                                "  [dim]AI 将被强制按步骤显式推理，并输出完整的思维链 (Chain of Thought)[/dim]"
                            )
                        continue
                    elif cmd.startswith("/spec"):
                        parts = prompt.split(maxsplit=1)
                        spec_query = parts[1] if len(parts) > 1 else ""
                        if not spec_query:
                            # 无参数时读取并显示当前 SPEC.md
                            spec_path = workspace_dir / ".foxcode" / "SPEC.md"
                            if spec_path.exists():
                                content = spec_path.read_text(encoding="utf-8")
                                console.print(
                                    Panel(
                                        Markdown(content),
                                        title="[bold cyan]当前规格说明[/bold cyan]",
                                        border_style="cyan",
                                    )
                                )
                            else:
                                console.print(
                                    "[yellow]暂无规格说明文件 (.foxcode/SPEC.md)，"
                                    "可使用 /spec <需求描述> 生成[/yellow]"
                                )
                        else:
                            # 有参数时进入 spec 生成流程
                            spec_prompt = (
                                "[Spec mode] STRICT RULES:\n"
                                "1. FIRST investigate the codebase using read-only tools "
                                "(tree, read_file, search_symbols, etc.) to understand existing structure.\n"
                                "2. THEN produce a structured technical specification following this template:\n"
                                "   - 1. Requirements Overview (background, goals, scope)\n"
                                "   - 2. Technical Solution (stack, architecture, data flow)\n"
                                "   - 3. API Design (endpoints, parameters, auth)\n"
                                "   - 4. Data Model (entities, schema, state machine)\n"
                                "   - 5. Implementation Steps (phases, risks, rollback)\n"
                                "   - 6. Test Plan (unit/integration/performance coverage)\n"
                                "   - 7. Acceptance Criteria (checklist, non-functional metrics, deliverables)\n"
                                "3. You MUST call `generate_spec` to save the spec to .foxcode/SPEC.md. "
                                "Only writing to explanation is NOT enough — the user needs a saved file.\n"
                                "4. Do NOT write any implementation code, tests, or config files yet. "
                                "Only produce the specification document.\n\n"
                                f"Requirement: {spec_query}"
                            )
                            send_prompt = _expand_file_refs(spec_prompt, workspace_dir)
                            send_prompt = _parse_image_refs(send_prompt, workspace_dir)
                            try:
                                all_messages, plan = await _run_status_loop(
                                    agent, send_prompt, all_messages, deps, config
                                )
                                summary = deps.tool_tracker.summary_str()
                                if summary:
                                    console.print(
                                        f"  [bold cyan]工具调用: {summary}[/bold cyan]"
                                    )
                                print_action_plan(plan)
                                if plan.files_modified:
                                    await _show_colored_diff(
                                        workspace_dir, plan.files_modified
                                    )
                            except Exception as e:
                                _print_run_error(e)
                        continue
                    elif cmd == "/solo":
                        perms.solo_mode = not perms.solo_mode
                        status_text = "开启" if perms.solo_mode else "关闭"
                        console.print(
                            f"[yellow]无人值守(Solo)模式 {status_text}[/yellow]\n"
                            "  [dim]高危命令仍会自动拦截，其他操作不再询问[/dim]"
                        )
                        continue
                    elif cmd == "/permissions":
                        console.print(f"[cyan]{perms.summary()}[/cyan]")
                        continue
                    elif cmd == "/free":
                        config["base_url"] = BUILTIN_FREE_BASE_URL
                        config["api_key"] = BUILTIN_FREE_API_KEY
                        selected = await _select_model_interactive(
                            config["base_url"],
                            config["api_key"],
                            config.get("model") or "",
                        )
                        if selected:
                            config["model"] = selected
                        settings_path = workspace_dir / ".foxcode" / "settings.json"
                        try:
                            if settings_path.exists():
                                settings = json.loads(
                                    settings_path.read_text(encoding="utf-8")
                                )
                            else:
                                settings = {}
                        except Exception:
                            settings = {}
                        settings["base_url"] = config["base_url"]
                        settings["api_key"] = config["api_key"]
                        settings["model"] = config["model"]
                        try:
                            settings_path.parent.mkdir(parents=True, exist_ok=True)
                            settings_path.write_text(
                                json.dumps(settings, ensure_ascii=False, indent=2),
                                encoding="utf-8",
                            )
                        except Exception as e:
                            console.print(f"  [yellow]保存配置失败: {e}[/yellow]")
                        new_agent = create_agent(
                            config,
                            http_client,
                            project_config["instructions"],
                            mcp_toolsets=mcp_toolsets,
                            skills_list=skills_list,
                            subagent_list=subagent_list,
                            rules=project_config["rules"],
                            memory=project_config["memory"],
                        )
                        if mcp_toolsets:
                            try:
                                await new_agent.__aenter__()
                            except Exception as e:
                                console.print(
                                    f"  [yellow]新 Agent MCP 初始化失败: {e}[/yellow]"
                                )
                        agent = new_agent
                        console.print(
                            f"[green]已切换到内置免费 API，当前模型: {config['model']}[/green]"
                        )
                        continue
                    elif cmd == "/openai":
                        env_config = load_config()
                        for key in ("model", "base_url", "api_key"):
                            config[key] = env_config[key]

                        settings_path = workspace_dir / ".foxcode" / "settings.json"
                        try:
                            if settings_path.exists():
                                settings = json.loads(
                                    settings_path.read_text(encoding="utf-8")
                                )
                            else:
                                settings = {}
                        except Exception:
                            settings = {}
                        for key in ("model", "base_url", "api_key"):
                            settings.pop(key, None)
                        try:
                            settings_path.parent.mkdir(parents=True, exist_ok=True)
                            settings_path.write_text(
                                json.dumps(settings, ensure_ascii=False, indent=2),
                                encoding="utf-8",
                            )
                        except Exception as e:
                            console.print(f"  [yellow]保存配置失败: {e}[/yellow]")

                        new_agent = create_agent(
                            config,
                            http_client,
                            project_config["instructions"],
                            mcp_toolsets=mcp_toolsets,
                            skills_list=skills_list,
                            subagent_list=subagent_list,
                            rules=project_config["rules"],
                            memory=project_config["memory"],
                        )
                        if mcp_toolsets:
                            try:
                                await new_agent.__aenter__()
                            except Exception as e:
                                console.print(
                                    f"  [yellow]新 Agent MCP 初始化失败: {e}[/yellow]"
                                )
                        agent = new_agent
                        console.print(
                            f"[green]已切换回 .env 配置，当前模型: {config['model']}[/green]"
                        )
                        continue
                    elif cmd == "/model":
                        console.print(
                            "[dim]兼容 openai-url 配置，直接回车保留原参数[/dim]"
                        )
                        new_url = console.input(
                            f"[bold cyan]API URL [/bold cyan][dim]({config.get('base_url', '')})[/dim]: "
                        ).strip()
                        new_key = console.input(
                            "[bold cyan]API Key [/bold cyan][dim](留空保留原值)[/dim]: "
                        ).strip()
                        new_model = console.input(
                            f"[bold cyan]模型名称 [/bold cyan][dim]({config.get('model', '')})[/dim]: "
                        ).strip()

                        settings_path = workspace_dir / ".foxcode" / "settings.json"
                        try:
                            if settings_path.exists():
                                settings = json.loads(
                                    settings_path.read_text(encoding="utf-8")
                                )
                            else:
                                settings = {}
                        except Exception:
                            settings = {}

                        updated = False
                        if new_url:
                            config["base_url"] = new_url
                            settings["base_url"] = new_url
                            updated = True
                        if new_key:
                            config["api_key"] = new_key
                            settings["api_key"] = new_key
                            updated = True
                        if new_model:
                            config["model"] = new_model
                            settings["model"] = new_model
                            updated = True

                        if updated:
                            try:
                                settings_path.parent.mkdir(parents=True, exist_ok=True)
                                settings_path.write_text(
                                    json.dumps(settings, ensure_ascii=False, indent=2),
                                    encoding="utf-8",
                                )
                                console.print("[green]已保存模型配置[/green]")
                            except Exception as e:
                                console.print(f"[red]保存失败: {e}[/red]")
                        else:
                            console.print("[dim]未修改任何参数[/dim]")
                        continue
                    elif cmd == "/mcp":
                        if mcp_toolsets:
                            console.print(
                                "[cyan]已配置 MCP 服务器:[/cyan]\n  "
                                + "\n  ".join(t.id or "?" for t in mcp_toolsets)
                            )
                        else:
                            console.print(
                                "[yellow]未配置 MCP 服务器（可创建 .foxcode/mcp.json）[/yellow]"
                            )
                        continue
                    elif cmd == "/skills":
                        if skills_mgr.list():
                            table = Table(title="可用 Skills", box=box.SIMPLE)
                            table.add_column("名称", style="cyan")
                            table.add_column("说明", style="white")
                            for s in skills_mgr.list():
                                table.add_row(s.name, s.description)
                            console.print(table)
                        else:
                            console.print(
                                "[yellow]暂无 Skills（可创建 .foxcode/skills/<name>/SKILL.md）[/yellow]"
                            )
                        continue
                    elif cmd.startswith("/skill "):
                        parts = cmd.split(maxsplit=1)
                        if len(parts) < 2:
                            console.print("[yellow]用法: /skill <名称>[/yellow]")
                        else:
                            skill_name = parts[1].strip().split()[0]
                            skill = skills_mgr.get(skill_name)
                            if skill is None:
                                console.print(f"[red]未找到 skill: {skill_name}[/red]")
                            else:
                                pending_skill = skill.content
                                console.print(
                                    f"[green]已注入 skill: {skill.name}，将在下一条提示生效[/green]"
                                )
                        continue
                    elif cmd == "/agents":
                        if subagents_mgr.list():
                            table = Table(title="可用子代理", box=box.SIMPLE)
                            table.add_column("名称", style="cyan")
                            table.add_column("说明", style="white")
                            for d in subagents_mgr.list():
                                table.add_row(d.name, d.description or "-")
                            console.print(table)
                        else:
                            console.print(
                                "[yellow]暂无子代理（可创建 .foxcode/agents/<name>.md）[/yellow]"
                            )
                        continue
                    elif cmd == "/term":
                        terminal_mode = not terminal_mode
                        status_text = "开启" if terminal_mode else "关闭"
                        console.print(
                            f"[yellow]终端模式 {status_text} (Ctrl+X 切换)[/yellow]"
                        )
                        continue
                    elif cmd == "/clear":
                        console.clear()
                        print_welcome()
                        continue
                    elif cmd == "/history":
                        show_history(deps)
                        continue
                    elif cmd == "/usage":
                        u = deps.tool_tracker.usage_summary(config["model"])
                        console.print(f"[cyan]会话用量统计:[/cyan]\n  {u}")
                        continue
                    elif cmd.startswith("/session"):
                        parts = cmd.split(maxsplit=2)
                        action = parts[1] if len(parts) > 1 else ""
                        if action == "list":
                            sessions = session_manager.list_sessions()
                            if not sessions:
                                console.print("[yellow]暂无保存的会话[/yellow]")
                            else:
                                table = Table(title="已保存的会话", box=box.SIMPLE)
                                table.add_column("名称", style="cyan")
                                table.add_column("消息数", style="white")
                                table.add_column("保存时间", style="dim")
                                for s in sessions:
                                    table.add_row(
                                        s["name"],
                                        str(s["size"])
                                        if isinstance(s["size"], int)
                                        else "?",
                                        s["modified"],
                                    )
                                console.print(table)
                        elif action == "save":
                            raw_parts = prompt.split(maxsplit=2)
                            name = (
                                raw_parts[2]
                                if len(raw_parts) > 2
                                else session_manager.get_auto_save_name()
                            )
                            result = session_manager.save_session(name, all_messages)
                            console.print(f"[green]{result}[/green]")
                        elif action == "load":
                            raw_parts = prompt.split(maxsplit=2)
                            name = raw_parts[2] if len(raw_parts) > 2 else ""
                            if not name:
                                console.print(
                                    "[yellow]用法: /session load <名称>[/yellow]"
                                )
                            else:
                                loaded = session_manager.load_session(name)
                                if loaded is None:
                                    console.print(f"[red]未找到会话: {name}[/red]")
                                else:
                                    all_messages.clear()
                                    all_messages.extend(loaded)
                                    console.print(
                                        f"[green]已加载会话: {name} ({len(loaded)} 条消息)[/green]"
                                    )
                        elif action in ("del", "delete", "rm"):
                            raw_parts = prompt.split(maxsplit=2)
                            name = raw_parts[2] if len(raw_parts) > 2 else ""
                            if not name:
                                console.print(
                                    "[yellow]用法: /session del <名称>[/yellow]"
                                )
                            elif session_manager.delete_session(name):
                                console.print(f"[green]已删除会话: {name}[/green]")
                            else:
                                console.print(f"[red]删除失败: {name}[/red]")
                        else:
                            console.print(
                                "[yellow]用法: /session list|save [名称]|load <名称>|del <名称>[/yellow]"
                            )
                        continue
                    elif cmd.startswith("/export"):
                        parts = prompt.split(maxsplit=1)
                        default_name = (
                            f"session_{session_manager.get_auto_save_name()}.md"
                        )
                        out_name = parts[1] if len(parts) > 1 else default_name
                        try:
                            from .tools.file_ops import _resolve_safe_path

                            out_path = _resolve_safe_path(workspace_dir, out_name)
                        except ValueError as e:
                            console.print(f"[red]导出路径非法: {e}[/red]")
                            continue
                        try:
                            lines = ["# FoxCode 会话导出\n"]
                            for i, msg in enumerate(all_messages, 1):
                                role = "unknown"
                                content = ""
                                if hasattr(msg, "kind"):
                                    if msg.kind == "request":
                                        role = "user"
                                        if hasattr(msg, "parts"):
                                            for part in msg.parts:
                                                if hasattr(part, "content"):
                                                    content += str(part.content)
                                        elif hasattr(msg, "content"):
                                            content = str(msg.content)
                                    elif msg.kind == "response":
                                        role = "assistant"
                                        if hasattr(msg, "parts"):
                                            for part in msg.parts:
                                                if hasattr(part, "content"):
                                                    content += str(part.content)
                                        elif hasattr(msg, "output"):
                                            content = str(msg.output)
                                        elif hasattr(msg, "data"):
                                            content = str(msg.data)
                                elif hasattr(msg, "role"):
                                    role = msg.role
                                    if hasattr(msg, "content"):
                                        content = str(msg.content)
                                elif isinstance(msg, dict):
                                    role = msg.get("role", "unknown")
                                    content = str(
                                        msg.get("content", msg.get("data", ""))
                                    )
                                else:
                                    content = str(msg)
                                if content.strip():
                                    lines.append(f"## [{i}] {role}\n")
                                    lines.append(f"{content.strip()}\n")
                            out_path.write_text("\n".join(lines), encoding="utf-8")
                            console.print(f"[green]会话已导出: {out_path}[/green]")
                        except Exception as e:
                            console.print(f"[red]导出失败: {e}[/red]")
                        continue
                    elif cmd.startswith("/undo"):
                        parts = cmd.split()
                        steps = 1
                        if len(parts) > 1 and parts[1].isdigit():
                            steps = int(parts[1])
                        run_undo(deps, steps)
                        continue
                    elif cmd.startswith("/commit"):
                        parts = cmd.split(maxsplit=1)
                        msg = parts[1] if len(parts) > 1 else ""
                        console.print("[dim]暂存所有变更...[/dim]")
                        add_result = await _exec_shell(
                            "git add .", workspace_dir, config["shell_timeout"]
                        )
                        diff = await _exec_shell(
                            "git diff --cached",
                            workspace_dir,
                            config["shell_timeout"],
                        )
                        if "退出码" in add_result and "没有" not in add_result:
                            console.print(f"[red]git add 失败: {add_result}[/red]")
                            continue
                        if (
                            not diff.strip()
                            or "退出码" in diff
                            or diff.strip() == "(命令执行成功，无输出)"
                        ):
                            console.print("[yellow]没有检测到变更，无需提交[/yellow]")
                            continue
                        if msg:
                            result = await _exec_shell_args(
                                ["git", "commit", "-m", msg],
                                workspace_dir,
                                config["shell_timeout"],
                            )
                            console.print(f"[green]{result}[/green]")
                        else:
                            stat = await _exec_shell(
                                "git diff --cached --stat",
                                workspace_dir,
                                config["shell_timeout"],
                            )
                            console.print(f"[cyan]变更文件:[/cyan]\n{stat}")
                            with console.status(
                                "[yellow]AI 正在生成提交信息...[/yellow]"
                            ):
                                ai_msg = await _generate_commit_message(
                                    http_client, config, diff
                                )
                            if ai_msg:
                                console.print(
                                    f"[green]生成提交信息:[/green] [bold]{ai_msg}[/bold]"
                                )
                                result = await _exec_shell_args(
                                    ["git", "commit", "-m", ai_msg],
                                    workspace_dir,
                                    config["shell_timeout"],
                                )
                                console.print(f"[green]{result}[/green]")
                            else:
                                console.print(
                                    "[yellow]AI 生成失败，请输入提交信息:[/yellow]"
                                )
                                manual_msg = console.input(
                                    "[bold cyan]提交信息: [/bold cyan]"
                                ).strip()
                                if manual_msg:
                                    result = await _exec_shell_args(
                                        ["git", "commit", "-m", manual_msg],
                                        workspace_dir,
                                        config["shell_timeout"],
                                    )
                                    console.print(f"[green]{result}[/green]")
                                else:
                                    console.print("[red]提交已取消[/red]")
                        continue
                    else:
                        custom_found = False
                        for cname, cprompt in project_config["commands"].items():
                            if cmd == f"/{cname}" or cmd.startswith(f"/{cname} "):
                                prompt = cprompt
                                console.print(f"[dim]执行自定义命令: {cname}[/dim]")
                                custom_found = True
                                break
                        if not custom_found:
                            console.print(
                                f"[red]未知命令: {cmd} (输入 /help 查看可用命令)[/red]"
                            )
                            continue

                try:
                    deps.tool_tracker.reset()
                    console.print("[dim]────────────────────────────────────────[/dim]")

                    send_prompt = _expand_file_refs(prompt, workspace_dir)
                    if pending_skill:
                        send_prompt = (
                            f"Please read the following skill content first and strictly follow its guidance:\n\n"
                            f"---\n{pending_skill}\n---\n\n{send_prompt}"
                        )
                        pending_skill = None
                    if deps.plan_mode:
                        send_prompt = (
                            "[Plan mode] Use only read-only tools to investigate; do not modify files or run commands. "
                            "After investigating, give a clear step-by-step implementation plan in the ActionPlan.\n\n"
                            + send_prompt
                        )
                    if deps.cot_mode:
                        send_prompt = COT_INSTRUCTION + send_prompt

                    # 解析图片引用
                    send_prompt = _parse_image_refs(send_prompt, workspace_dir)

                    # 若会话历史已压缩，自动提示 AI 读取持久化上下文摘要
                    from .context_compressor import inject_context_hint

                    send_prompt = inject_context_hint(
                        send_prompt, workspace_dir, all_messages
                    )

                    all_messages, plan = await _run_status_loop(
                        agent, send_prompt, all_messages, deps, config
                    )

                    from .context_compressor import compress_messages

                    if (
                        len(all_messages) > max_history_messages
                        or token_estimator.estimate(all_messages) > max_context_tokens
                    ):
                        with console.status(
                            "[dim]智能压缩上下文中...[/dim]", spinner="fox"
                        ):
                            all_messages, summary_text = await compress_messages(
                                all_messages, http_client, config
                            )
                        if summary_text:
                            console.print(f"  [dim]{summary_text}[/dim]")

                    summary = deps.tool_tracker.summary_str()
                    if summary:
                        console.print(f"  [bold cyan]工具调用: {summary}[/bold cyan]")

                    print_action_plan(plan)
                    # 展示本轮变更摘要
                    if plan.files_modified:
                        await _show_colored_diff(workspace_dir, plan.files_modified)

                    usage_summary = deps.tool_tracker.usage_summary(config["model"])
                    if usage_summary:
                        console.print(f"  [dim]用量: {usage_summary}[/dim]")

                except Exception as e:
                    _print_run_error(e)

        if mcp_toolsets:
            try:
                async with agent:
                    await _run_loop()
            except Exception as e:
                console.print(f"[yellow]⚠ MCP 初始化失败: {e}[/yellow]\n")
                agent = create_agent(
                    config,
                    http_client,
                    project_config["instructions"],
                    mcp_toolsets=None,
                    skills_list=skills_list,
                    subagent_list=subagent_list,
                    rules=project_config["rules"],
                    memory=project_config["memory"],
                )
                async with agent:
                    await _run_loop()
        else:
            await _run_loop()


# NOTE:异步主入口：解析参数、加载配置、区分 headless 与交互模式并分发执行