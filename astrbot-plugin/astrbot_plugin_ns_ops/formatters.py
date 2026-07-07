def clamp_text(text: str, max_chars: int) -> str:
    value = str(text or "").strip()
    if len(value) <= max_chars:
        return value
    return value[:max_chars] + "\n...[已截断]"


def format_help() -> str:
    return "\n".join(
        [
            "NS 运维入口",
            "",
            "只读命令：",
            "/ns ping",
            "/ns status",
            "/ns health",
            "/ns daily",
            "/ns logs astrbot",
            "/ns traffic today",
            "/ns traffic debug",
            "/ns traffic status",
            "/ns traffic bind",
            "/ns v2 status",
            "/ns v2 check",
            "/ns v2 build",
            "/ns armoire check-store",
            "/ns armoire audit-store",
            "/ns armoire audit-store-latest",
            "/ns git status",
            "/ns git diff",
            "",
            "需要确认：",
            "/ns restart astrbot",
            "/ns v2 deploy",
            "/ns git commit <提交说明>",
            "/ns git push",
            "/ns file write <文件名.md> <内容>",
            "/ns armoire sync-catalog",
            "/ns confirm <验证码>",
        ]
    )


def format_health(payload: dict, max_chars: int) -> str:
    if not payload.get("ok"):
        return "NS Ops Runner 不可用：\n" + clamp_text(payload.get("error", "unknown error"), max_chars)

    paths = payload.get("paths") or {}
    lines = [
        "NS Ops Runner 在线",
        f"version: {payload.get('version', '-')}",
        f"pid: {payload.get('pid', '-')}",
        f"uptime: {payload.get('uptimeSeconds', '-')}s",
    ]
    if paths:
        lines.extend(
            [
                "",
                "paths:",
                f"v2: {paths.get('V2_ROOT', '-')}",
                f"astrbot: {paths.get('ASTRBOT_ROOT', '-')}",
            ]
        )
    return clamp_text("\n".join(lines), max_chars)


def format_armoire_latest_output(output: str, max_chars: int) -> str:
    lines = [line.rstrip() for line in str(output or "").splitlines()]
    header: list[str] = []
    blocks: list[list[str]] = []
    current: list[str] = []
    in_items = False

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("# "):
            continue
        if stripped == "重点项：":
            in_items = True
            continue
        if stripped.startswith("说明："):
            break
        if not in_items:
            if not stripped.startswith("catalogPath："):
                header.append(stripped)
            continue
        if stripped.startswith("- ["):
            if current:
                blocks.append(current)
            current = [stripped]
            continue
        if current and stripped:
            current.append(stripped)

    if current:
        blocks.append(current)

    actionable_prefixes = ("- [需人工确认]", "- [可补链接]", "- [已匹配，缺台服链接]")
    actionable = [block for block in blocks if block and block[0].startswith(actionable_prefixes)]

    result_lines = header[:5]
    result_lines.extend(["", "需要处理："])
    if actionable:
        for block in actionable[:8]:
            result_lines.append(block[0])
            if len(block) > 1:
                result_lines.append(block[1])
    else:
        result_lines.append("- 暂无需人工处理项。")

    if len(actionable) > 8:
        result_lines.append(f"- 还有 {len(actionable) - 8} 项未显示，可到 runner 日志看完整结果。")

    result_lines.extend(["", "说明：只读审核，不会修改 armoire-store-catalog.json。"])
    return clamp_text("\n".join(result_lines), min(max_chars, 1800))


def format_job_response(payload: dict, max_chars: int) -> str:
    if payload.get("confirmationRequired"):
        confirmation = payload.get("confirmation") or {}
        lines = [
            f"需要确认：{payload.get('title', payload.get('jobId', '-'))}",
            f"任务：{payload.get('jobId', '-')}",
        ]
        preview = clamp_text(payload.get("preview", ""), max_chars)
        if preview:
            lines.extend(["", "预览：", preview])
        lines.extend(
            [
                "",
                f"验证码：{confirmation.get('token', '-')}",
                f"有效期：{confirmation.get('ttlSeconds', '-')} 秒",
                "",
                f"/ns confirm {confirmation.get('token', '')}",
            ]
        )
        return "\n".join(lines).strip()

    if not payload.get("ok"):
        return "执行失败：\n" + clamp_text(payload.get("error", "unknown error"), max_chars)

    result = payload.get("result") or {}
    status = "成功" if result.get("ok") else "失败"
    raw_output = result.get("output") or result.get("summary") or ""
    if (
        result.get("jobId") in {"armoire.audit-store-latest", "armoire.audit-store"}
        and "NSArmoire 最新商城补全审核" in raw_output
    ):
        output = format_armoire_latest_output(raw_output, max_chars)
    else:
        output = result.get("summary") or result.get("output") or ""
    lines = [
        f"{status}: {result.get('title', result.get('jobId', '-'))}",
        f"耗时: {result.get('durationMs', 0)}ms",
    ]
    if output:
        lines.extend(["", clamp_text(output, max_chars)])
    return "\n".join(lines)
