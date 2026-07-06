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
            "/ns logs astrbot",
            "/ns v2 status",
            "/ns v2 check",
            "/ns v2 build",
            "/ns armoire check-store",
            "/ns armoire audit-store",
            "/ns git status",
            "/ns git diff",
            "",
            "需要确认：",
            "/ns restart astrbot",
            "/ns v2 deploy",
            "/ns git commit <提交说明>",
            "/ns git push",
            "/ns file write <文件名.md> <内容>",
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
    output = result.get("summary") or result.get("output") or ""
    lines = [
        f"{status}: {result.get('title', result.get('jobId', '-'))}",
        f"耗时: {result.get('durationMs', 0)}ms",
    ]
    if output:
        lines.extend(["", clamp_text(output, max_chars)])
    return "\n".join(lines)
