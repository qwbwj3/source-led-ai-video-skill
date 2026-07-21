#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

import precheck_scan
from lib.common import (
    SKILL_ROOT,
    WorkflowError,
    canonical_json,
    ensure_managed_dir,
    load_and_validate_config,
    managed_atomic_write_json,
    managed_atomic_write_text,
    managed_path,
    narration_hash,
    narration_text,
    normalize_text,
    read_json,
    sha256_bytes,
    sha256_file,
)


REVIEWER = "yuwen-publish-precheck"
RIGHTS_CONFIRMED = "confirmed"
RIGHTS_PENDING = "pending"
POLICY_REQUIRED = (
    "source-skill-contract.md",
    "workflow.md",
    "judgment.md",
    "rules-common.md",
    "rules-commercial.md",
    "platform-douyin.md",
    "industry-medical.md",
    "industry-finance.md",
    "repair.md",
    "my-rules.md",
    "profile.md",
    "expressions.md",
)
MEDICAL_SCOPE_ANCHORS = (
    re.compile(
        r"(?:医生|医师|医院|门诊|问诊|就医|挂号|疾病|病症|癌症|肿瘤|糖尿病|"
        r"高血压|高血糖|抑郁症|焦虑症|药品|药物|用药|处方药|非处方药|"
        r"减肥药|保健品|医疗器械|医美|整形手术|疫苗)"
    ),
    re.compile(r"医疗(?:科普|健康|服务|建议|机构|广告|内容|账号|模型|助手)"),
    re.compile(r"(?:治疗|诊断|疗效|治愈).{0,8}(?:疾病|病症|患者|症状)"),
    re.compile(r"(?:疾病|病症|患者|症状).{0,8}(?:治疗|诊断|疗效|治愈)"),
)
FINANCE_SCOPE_ANCHORS = (
    re.compile(
        r"(?:股票|个股|荐股|炒股|证券|期货|外汇|理财|贷款|信贷|收益率|"
        r"房贷|车贷|虚拟货币|加密货币|比特币|以太坊|债务优化|协商还款|"
        r"反催收|全额退保|征信修复|年化收益|保本收益)"
    ),
    re.compile(
        r"(?:(?:股票|指数|货币|债券|证券|公募|私募)基金|"
        r"基金(?!会|项目|资助)|基金(?:定投|净值|收益|申购|赎回|经理|产品))"
    ),
    re.compile(r"(?:买保险|卖保险|保险(?:公司|产品|理赔|保单|投保|代理|经纪|收益|理财))"),
    re.compile(r"金融(?:产品|服务|行业|投资|市场|机构|信息|建议|模型|工具)"),
    re.compile(r"(?:投资建议|投资组合|投资标的|投资收益率|投资回报率)"),
    re.compile(r"投资.{0,6}(?:买入|卖出)"),
)
COMMERCIAL_SCOPE_ANCHORS = (
    re.compile(r"(?:立即|现在|马上|赶紧|点击|去)(?:下单|购买|抢购|领券)"),
    re.compile(r"(?:点击|进入|打开).{0,10}(?:购物车|商品链接|购买链接|橱窗)"),
    re.compile(r"(?:购买链接|商品链接|加(?:我)?微信|添加微信|评论区领(?:取)?|私信我发)"),
    re.compile(r"(?:限时|限量).{0,8}(?:优惠|折扣|特价)"),
    re.compile(
        r"(?:立即|现在|马上|赶紧|点击|扫码|欢迎|开放|你可以|想学就)"
        r".{0,4}(?:报名|购买|加入|订阅).{0,8}(?:课程|训练营|会员|付费社群)"
    ),
    re.compile(
        r"(?:课程|训练营|会员|付费社群).{0,6}"
        r"(?:报名|购买|加入|订阅)(?:入口|链接|通道|开启|开放)"
    ),
    re.compile(
        r"(?:加(?:我)?微信|添加微信|扫码加我|私信(?:我)?).{0,12}"
        r"(?:购买|下单|报名|咨询|报价|领(?:取)?|领课|进群|合作|福利)"
    ),
    re.compile(r"评论区(?:扣|回复).{0,8}(?:领(?:取)?|获取|拿走|进群)"),
    re.compile(r"(?:扫码|扫描二维码).{0,10}(?:购买|报名|领券|咨询|进群)"),
    re.compile(r"(?:购物车|橱窗|小黄车).{0,6}(?:下单|购买|领券)"),
    re.compile(r"(?:带货|广告合作|商业合作|品牌赞助|返佣|佣金|推广码|优惠码)"),
    re.compile(r"(?:到手价|售价|团购价|早鸟价)\s*[¥￥]?\s*\d"),
)
REPORT_SECTION_TITLES = (
    "审核范围",
    "逐平台结论",
    "词面候选复核",
    "必改",
    "建议改",
    "仅提示",
    "无法判定",
    "发布前检查单",
    "边界声明",
)
REPORT_TRUST_STATEMENT = (
    "审核声明：已由 Agent 阅读完整口播、封面钩子、词面扫描结果和适用规则并完成语义判断；"
    "机器门禁只校验结构、范围一致性和证据绑定，不能证明语义判断本身正确。"
)
REPORT_BOUNDARY_STATEMENT = (
    "边界声明：“可以发”仅表示本次检查范围内未发现阻断项，不承诺平台审核通过，"
    "也不替代事实、资质、版权和授权核验。"
)


def policy_fingerprint() -> dict[str, str]:
    policy_root = SKILL_ROOT / "references/publish-precheck"
    required = [policy_root / name for name in POLICY_REQUIRED]
    paths = [
        SKILL_ROOT / "scripts/review_gate.py",
        SKILL_ROOT / "scripts/precheck_scan.py",
        SKILL_ROOT / "scripts/precheck_terms.json",
        *sorted(policy_root.rglob("*.md")),
    ]
    if any(not path.is_file() for path in required) or any(not path.is_file() for path in paths):
        raise WorkflowError("REVIEW_POLICY", "Bundled publish-precheck policy is incomplete")
    return {
        path.relative_to(SKILL_ROOT).as_posix(): sha256_file(path)
        for path in paths
    }


def policy_sha256() -> str:
    return sha256_bytes(canonical_json(policy_fingerprint()))


def review_industries(config: dict) -> list[str]:
    scope = config.get("review_scope", {})
    if type(scope) is not dict:
        raise WorkflowError("REVIEW_SCOPE", "Review scope is invalid")
    industries = scope.get("industries", [])
    if (
        type(industries) is not list
        or any(type(item) is not str for item in industries)
        or industries != sorted(set(industries))
        or any(item not in {"medical", "finance"} for item in industries)
    ):
        raise WorkflowError("REVIEW_SCOPE", "Review industries are invalid")
    return industries


def _first_anchor(text: str, patterns: tuple[re.Pattern[str], ...]) -> str | None:
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            return normalize_text(match.group(0))[:40]
    return None


def validate_scope_sentinel(config: dict) -> None:
    """Fail closed when obvious content contradicts the declared review scope."""
    text = narration_text(config) + "\n" + normalize_text(config["cover"]["hook"])
    industries = set(review_industries(config))
    problems: list[str] = []
    medical = _first_anchor(text, MEDICAL_SCOPE_ANCHORS)
    finance = _first_anchor(text, FINANCE_SCOPE_ANCHORS)
    commercial = _first_anchor(text, COMMERCIAL_SCOPE_ANCHORS)
    if medical and "medical" not in industries:
        problems.append(
            f'发现明显医疗锚点“{medical}”，请在 project.json 的 '
            'review_scope.industries 中加入 "medical"'
        )
    if finance and "finance" not in industries:
        problems.append(
            f'发现明显金融锚点“{finance}”，请在 project.json 的 '
            'review_scope.industries 中加入 "finance"'
        )
    if commercial and config.get("commercial", True) is False:
        problems.append(
            f'发现明显营销动作“{commercial}”，请把 project.json 的 commercial 改为 true'
        )
    if problems:
        raise WorkflowError(
            "REVIEW_SCOPE_SENTINEL",
            "；".join(problems) + "，然后重新运行 prepare 和 scan",
        )


def review_payload(config: dict) -> dict:
    validate_scope_sentinel(config)
    exact_narration = narration_text(config) + "\n"
    base = {
        "schema": 2,
        "platform": "douyin",
        "commercial": bool(config.get("commercial", True)),
        "industries": review_industries(config),
        "narration_sha256": narration_hash(config),
        "narration_text_sha256": sha256_bytes(exact_narration.encode("utf-8")),
        "cover_hook": normalize_text(config["cover"]["hook"]),
        "scene_order": [scene["id"] for scene in config["narration"]["scenes"]],
        "policy_sha256": policy_sha256(),
    }
    return base | {"review_payload_sha256": sha256_bytes(canonical_json(base))}


def _require_current_evidence(config: dict, project_root: Path) -> tuple[dict, dict, Path]:
    try:
        review_dir = managed_path(project_root, "review", must_exist=True, kind="dir")
        prepared_path = managed_path(
            project_root, review_dir / "review-input.json", must_exist=True, kind="file"
        )
        narration_path = managed_path(
            project_root, review_dir / "narration.txt", must_exist=True, kind="file"
        )
        scan_path = managed_path(
            project_root, review_dir / "lexical-scan.json", must_exist=True, kind="file"
        )
    except WorkflowError as exc:
        if exc.code == "MANAGED_MISSING":
            raise WorkflowError(
                "REVIEW_MISSING", "Run prepare and lexical precheck before approval"
            ) from exc
        raise
    if not (prepared_path.is_file() and narration_path.is_file() and scan_path.is_file()):
        raise WorkflowError("REVIEW_MISSING", "Run prepare and lexical precheck before approval")

    current = review_payload(config)
    prepared = read_json(prepared_path)
    if prepared != current:
        raise WorkflowError("REVIEW_STALE", "Prepared review payload does not match current content")
    exact_narration = (narration_text(config) + "\n").encode("utf-8")
    try:
        actual_narration = narration_path.read_bytes()
    except OSError as exc:
        raise WorkflowError("REVIEW_MISSING", "Narration review input is unreadable") from exc
    if actual_narration != exact_narration:
        raise WorkflowError("REVIEW_STALE", "Narration file does not match the current project")
    if sha256_file(narration_path) != current["narration_text_sha256"]:
        raise WorkflowError("REVIEW_STALE", "Narration review hash is stale")

    scan = read_json(scan_path)
    expected_scan = precheck_scan.scan(
        actual_narration.decode("utf-8"),
        commercial=current["commercial"],
        industries=set(current["industries"]),
        radius=24,
    )
    if scan != expected_scan:
        raise WorkflowError("REVIEW_SCAN_STALE", "Lexical scan does not match the exact review payload")
    return current, scan, scan_path


def _report_section(report_text: str, title: str) -> str:
    matches = re.findall(
        rf"(?ms)^##\s+{re.escape(title)}\s*\n(.*?)(?=^##\s+|\Z)",
        report_text,
    )
    if len(matches) != 1 or not normalize_text(matches[0]):
        raise WorkflowError(
            "REVIEW_REPORT", f"Semantic report needs one non-empty '{title}' section"
        )
    return matches[0].strip()


def _report_value(section: str, label: str, code: str) -> str:
    matches = re.findall(
        rf"(?m)^-\s*{re.escape(label)}：\s*([^\r\n]+?)\s*$",
        section,
    )
    if len(matches) != 1 or not normalize_text(matches[0]):
        raise WorkflowError(code, f"Semantic report needs one '{label}' scope value")
    return normalize_text(matches[0])


def _validate_semantic_report(
    report_text: str,
    result: str,
    payload: dict,
    scan: dict,
) -> str:
    payload_hash = payload["review_payload_sha256"]
    marker = re.compile(
        rf"(?m)^-?\s*Review-Payload-SHA256:\s*{re.escape(payload_hash)}\s*$"
    )
    if len(marker.findall(report_text)) != 1:
        raise WorkflowError("REVIEW_PAYLOAD", "Semantic report is not bound to this review payload")

    sections = {
        title: _report_section(report_text, title) for title in REPORT_SECTION_TITLES
    }
    scope = sections["审核范围"]
    expected_industries = "、".join(payload["industries"]) or "无"
    expected_commercial = "有" if payload["commercial"] else "无"
    expected_platform = "抖音" if payload["platform"] == "douyin" else payload["platform"]
    if _report_value(scope, "平台", "REVIEW_PLATFORM") != expected_platform:
        raise WorkflowError("REVIEW_PLATFORM", "Report platform does not match the review payload")
    if _report_value(scope, "商业属性", "REVIEW_SCOPE") != expected_commercial:
        raise WorkflowError("REVIEW_SCOPE", "Report commercial scope does not match the review payload")
    if _report_value(scope, "强监管行业", "REVIEW_SCOPE") != expected_industries:
        raise WorkflowError("REVIEW_SCOPE", "Report industry scope does not match the review payload")
    if _report_value(scope, "审核内容", "REVIEW_SCOPE") != "口播文案、封面钩子":
        raise WorkflowError("REVIEW_SCOPE", "Report must cover narration and cover hook")
    if not marker.search(scope):
        raise WorkflowError("REVIEW_PAYLOAD", "Payload hash must appear inside the review scope")

    conclusions = [line.strip() for line in re.findall(r"(?m)^结论[：:].*$", report_text)]
    platform_conclusion = _report_value(
        sections["逐平台结论"], "抖音", "REVIEW_PLATFORM"
    )
    if result == "pass":
        if conclusions != ["结论：可以发"] or platform_conclusion != "可以发":
            raise WorkflowError("REVIEW_RESULT", "Pass report needs one standalone '结论：可以发' line")
    elif result == "revised-pass":
        allowed = len(conclusions) == 1 and bool(
            re.fullmatch(r"结论：(?:改后可发|改完.{0,40}可以发)", conclusions[0])
        )
        if (
            not allowed
            or platform_conclusion != "改后可发"
            or len(re.findall(r"(?m)^复检：通过\s*$", report_text)) != 1
            or "已修复" not in sections["必改"]
        ):
            raise WorkflowError("REVIEW_RESULT", "Revised pass requires one repaired conclusion and passed recheck")
    else:
        raise WorkflowError("REVIEW_RESULT", "Only pass or revised-pass can unlock TTS")

    counts = {
        name: len(scan[name])
        for name in ("candidates", "personal_hits", "myth_advisories", "warnings")
    }
    lexical_marker = (
        "Lexical-Review: complete; "
        + "; ".join(f"{name}={counts[name]}" for name in counts)
    )
    lexical = sections["词面候选复核"]
    complete_declaration = bool(
        re.search(rf"(?m)^-?\s*{re.escape(lexical_marker)}\s*$", lexical)
        and re.search(r"词面候选不等于违规结论.*零候选不等于语义安全", lexical)
    )
    if not complete_declaration:
        expected_dispositions = {
            f"{name}[{index}]"
            for name in ("candidates", "personal_hits", "myth_advisories")
            for index in range(counts[name])
        }
        found_dispositions = set(
            re.findall(
                r"(?m)^-\s*Lexical-Disposition:\s*"
                r"((?:candidates|personal_hits|myth_advisories)\[\d+\])\s*\|"
                r"\s*(?:必改|建议改|仅提示|无法判定)\s*\|\s*\S.+$",
                lexical,
            )
        )
        warnings_marker = f"Lexical-Warnings-Reviewed: {counts['warnings']}"
        if (
            not expected_dispositions
            or found_dispositions != expected_dispositions
            or not re.search(rf"(?m)^-?\s*{re.escape(warnings_marker)}\s*$", lexical)
        ):
            raise WorkflowError(
                "REVIEW_LEXICAL",
                "Report must attest complete lexical review with exact counts or disposition every candidate",
            )

    if result == "pass" and normalize_text(sections["必改"]) != "- 无":
        raise WorkflowError("REVIEW_RESULT", "A pass report cannot retain mandatory changes")
    if normalize_text(sections["无法判定"]) != "- 无":
        raise WorkflowError("REVIEW_RESULT", "Approval cannot retain unresolved judgments")

    checklist = sections["发布前检查单"]
    checklist_values = {
        label: _report_value(checklist, label, "REVIEW_CHECKLIST")
        for label in (
            "AI生成内容标注",
            "虚构演绎标注",
            "营销信息标注",
            "转载与来源标注",
            "事实证据",
            "素材授权",
        )
    }
    allowed_status = re.compile(r"(?:已确认|需要|不适用)(?:（[^）\r\n]+）)?")
    if any(not allowed_status.fullmatch(value) for value in checklist_values.values()):
        raise WorkflowError("REVIEW_CHECKLIST", "Publish checklist contains an invalid status")
    if checklist_values["AI生成内容标注"].startswith("不适用"):
        raise WorkflowError("REVIEW_CHECKLIST", "This AI workflow requires an AI-content label")
    if payload["commercial"] and checklist_values["营销信息标注"].startswith("不适用"):
        raise WorkflowError("REVIEW_CHECKLIST", "Commercial content needs a marketing-label action")
    if checklist_values["事实证据"] != "已确认":
        raise WorkflowError("REVIEW_CHECKLIST", "Factual evidence must be confirmed before approval")
    material_rights = checklist_values["素材授权"]
    if material_rights not in {"已确认", "需要（发布前确认）"}:
        raise WorkflowError(
            "REVIEW_CHECKLIST",
            "Material rights must be confirmed or explicitly retained as a pre-publication action",
        )

    boundary = sections["边界声明"]
    if REPORT_TRUST_STATEMENT not in boundary or REPORT_BOUNDARY_STATEMENT not in boundary:
        raise WorkflowError(
            "REVIEW_BOUNDARY", "Report must state the Agent-review trust boundary and platform limitation"
        )
    return RIGHTS_CONFIRMED if material_rights == "已确认" else RIGHTS_PENDING


def prepare(config_path: Path) -> dict:
    config, project_root = load_and_validate_config(config_path)
    validate_scope_sentinel(config)
    review_dir = ensure_managed_dir(project_root, "review")
    narration = narration_text(config)
    hook = normalize_text(config["cover"]["hook"])
    payload = review_payload(config)
    managed_atomic_write_text(project_root, review_dir / "narration.txt", narration + "\n")
    managed_atomic_write_text(
        project_root,
        review_dir / "review-content.md",
        "# 待审核内容\n\n"
        f"- 平台：抖音\n- 商业属性：{'有/按有处理' if payload['commercial'] else '无'}\n"
        f"- 强监管行业：{('、'.join(payload['industries']) or '无')}\n"
        f"- Review-Payload-SHA256: {payload['review_payload_sha256']}\n\n"
        "## 口播文案\n\n"
        + narration
        + "\n\n## 封面钩子\n\n"
        + hook
        + "\n",
    )
    managed_atomic_write_json(project_root, review_dir / "review-input.json", payload)
    return payload


def scan(config_path: Path) -> dict:
    config, project_root = load_and_validate_config(config_path)
    validate_scope_sentinel(config)
    review_dir = ensure_managed_dir(project_root, "review")
    narration_path = managed_path(
        project_root,
        review_dir / "narration.txt",
        must_exist=True,
        kind="file",
    )
    exact = narration_text(config) + "\n"
    try:
        actual = narration_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise WorkflowError("REVIEW_MISSING", "Narration review input is unreadable") from exc
    if actual != exact:
        raise WorkflowError("REVIEW_STALE", "Run prepare before the lexical scan")
    result = precheck_scan.scan(
        actual,
        commercial=bool(config.get("commercial", True)),
        industries=set(review_industries(config)),
        radius=24,
    )
    managed_atomic_write_json(
        project_root,
        review_dir / "lexical-scan.json",
        result,
    )
    return result


def approve(config_path: Path, report_path: Path, result: str, reviewer: str) -> dict:
    config, project_root = load_and_validate_config(config_path)
    validate_scope_sentinel(config)
    current, lexical_scan, scan_path = _require_current_evidence(config, project_root)
    if normalize_text(reviewer) != REVIEWER:
        raise WorkflowError("REVIEW_REVIEWER", "Approval must be issued by yuwen-publish-precheck")

    try:
        report = managed_path(
            project_root,
            Path(os.path.abspath(os.fspath(report_path))),
            must_exist=True,
            kind="file",
        )
    except WorkflowError as exc:
        if exc.code == "MANAGED_MISSING":
            raise WorkflowError("REVIEW_MISSING", "Semantic review report is missing") from exc
        raise
    try:
        report.relative_to(project_root.resolve() / "review")
    except ValueError as exc:
        raise WorkflowError("REVIEW_PATH", "Review report must be inside project/review") from exc
    if not report.is_file():
        raise WorkflowError("REVIEW_MISSING", "Semantic review report is missing")
    try:
        report_text = report.read_text(encoding="utf-8")
    except OSError as exc:
        raise WorkflowError("REVIEW_MISSING", "Semantic review report is unreadable") from exc
    rights_clearance = _validate_semantic_report(
        report_text, result, current, lexical_scan
    )

    prepared = managed_path(
        project_root, "review/review-input.json", must_exist=True, kind="file"
    )
    approval = {
        "schema": 2,
        "platform": "douyin",
        "result": result,
        "reviewer": REVIEWER,
        "review_payload_sha256": current["review_payload_sha256"],
        "policy_sha256": current["policy_sha256"],
        "narration_sha256": current["narration_sha256"],
        "review_input_sha256": sha256_file(prepared),
        "narration_text_sha256": current["narration_text_sha256"],
        "lexical_scan_sha256": sha256_file(scan_path),
        "semantic_report": str(report.relative_to(project_root)),
        "semantic_report_sha256": sha256_file(report),
        "rights_clearance": rights_clearance,
    }
    managed_atomic_write_json(project_root, "review/approval.json", approval)
    return approval


def _verify_approval_for_config(config: dict, project_root: Path) -> dict:
    validate_scope_sentinel(config)
    try:
        approval_path = managed_path(
            project_root, "review/approval.json", must_exist=True, kind="file"
        )
    except WorkflowError as exc:
        if exc.code == "MANAGED_MISSING":
            raise WorkflowError(
                "REVIEW_BLOCKED", "TTS blocked: review/approval.json is missing"
            ) from exc
        raise
    if not approval_path.is_file():
        raise WorkflowError("REVIEW_BLOCKED", "TTS blocked: review/approval.json is missing")
    approval = read_json(approval_path)
    if (
        not isinstance(approval, dict)
        or approval.get("schema") != 2
        or approval.get("result") not in {"pass", "revised-pass"}
        or approval.get("reviewer") != REVIEWER
        or approval.get("rights_clearance")
        not in {RIGHTS_CONFIRMED, RIGHTS_PENDING}
    ):
        raise WorkflowError("REVIEW_BLOCKED", "TTS blocked: approval result is invalid")

    current, lexical_scan, scan_path = _require_current_evidence(config, project_root)
    if (
        approval.get("review_payload_sha256") != current["review_payload_sha256"]
        or approval.get("policy_sha256") != current["policy_sha256"]
        or approval.get("narration_sha256") != current["narration_sha256"]
        or approval.get("narration_text_sha256") != current["narration_text_sha256"]
    ):
        raise WorkflowError("REVIEW_STALE", "TTS blocked: reviewed content changed after approval")
    prepared = managed_path(
        project_root, "review/review-input.json", must_exist=True, kind="file"
    )
    if (
        sha256_file(prepared) != approval.get("review_input_sha256")
        or sha256_file(scan_path) != approval.get("lexical_scan_sha256")
    ):
        raise WorkflowError("REVIEW_STALE", "TTS blocked: review evidence changed")

    semantic_report = approval.get("semantic_report")
    if type(semantic_report) is not str:
        raise WorkflowError("REVIEW_STALE", "TTS blocked: semantic review path is invalid")
    report = managed_path(
        project_root, semantic_report, must_exist=True, kind="file"
    )
    if not report.is_file() or sha256_file(report) != approval.get("semantic_report_sha256"):
        raise WorkflowError("REVIEW_STALE", "TTS blocked: semantic review report changed")
    try:
        report_text = report.read_text(encoding="utf-8")
    except OSError as exc:
        raise WorkflowError("REVIEW_STALE", "TTS blocked: semantic report is unreadable") from exc
    report_rights_clearance = _validate_semantic_report(
        report_text, approval["result"], current, lexical_scan
    )
    if approval.get("rights_clearance") != report_rights_clearance:
        raise WorkflowError(
            "REVIEW_STALE",
            "TTS blocked: approval rights clearance differs from the semantic report",
        )
    return approval


def require_confirmed_rights(approval: dict) -> None:
    """Block upload-ready delivery unless verified material rights are confirmed."""
    if approval.get("rights_clearance") != RIGHTS_CONFIRMED:
        raise WorkflowError(
            "RIGHTS_PENDING",
            "Final delivery blocked: material authorization still needs pre-publication confirmation",
        )


def verify_approval(config_path: Path) -> dict:
    config, project_root = load_and_validate_config(config_path)
    validate_scope_sentinel(config)
    return _verify_approval_for_config(config, project_root)


def verify_packaged_approval(config_path: Path) -> dict:
    """Verify bundled review evidence without requiring original media inputs."""
    config_path = config_path.resolve()
    config = read_json(config_path)
    if not isinstance(config, dict) or config.get("version") != 1:
        raise WorkflowError("REVIEW_CONFIG", "Packaged project config is invalid")
    validate_scope_sentinel(config)
    return _verify_approval_for_config(config, config_path.parent)


def main() -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--config", type=Path, required=True)
    lexical = sub.add_parser("scan")
    lexical.add_argument("--config", type=Path, required=True)
    app = sub.add_parser("approve")
    app.add_argument("--config", type=Path, required=True)
    app.add_argument("--report", type=Path, required=True)
    app.add_argument("--result", choices=("pass", "revised-pass"), required=True)
    app.add_argument("--reviewer", required=True)
    verify = sub.add_parser("verify")
    verify.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        result = prepare(args.config)
    elif args.command == "scan":
        result = scan(args.config)
    elif args.command == "approve":
        result = approve(args.config, args.report, args.result, args.reviewer)
    else:
        result = verify_approval(args.config)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except WorkflowError as exc:
        print(f"ERROR[{exc.code}]: {exc}", file=sys.stderr)
        raise SystemExit(2)
