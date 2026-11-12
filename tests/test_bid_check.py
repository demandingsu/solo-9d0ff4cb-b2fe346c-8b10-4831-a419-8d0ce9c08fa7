"""端到端测试：开标批次核对（目录 → 明细 → 汇总 → 差异/冲突/无法核定）。

运行方式：
    PYTHONPATH=/tmp/pylibs python3 -I tests/test_bid_check.py
"""
from __future__ import annotations

import functools
import http.server
import sys
import threading
from pathlib import Path

# -I 隔离模式下 PYTHONPATH 不生效，显式注入依赖目录
for candidate in ("/tmp/pylibs", "/tmp/pyroot/lib/python3.11/site-packages"):
    if Path(candidate).exists() and candidate not in sys.path:
        sys.path.insert(0, candidate)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "webapp"))
sys.path.insert(0, str(ROOT / "example-site"))

from app.bidcheck import (  # noqa: E402
    BatchStatus,
    _parse_money,
    parse_directory_page,
    verify_batch,
)
from generate_site import main as generate_site_main  # noqa: E402

SITE_DIR = ROOT / "example-site" / "site"


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:  # 不输出访问日志
        pass


class DetailPagingHandler(QuietHandler):
    """把 B2026-001/SG-01 的明细拆成两页，验证明细页“下一页”会被继续读取。"""

    PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>SG-01 唱标明细</title></head>
<body>
<table>
<tr><th>批次号</th><td>B2026-001</td></tr>
<tr><th>批次修订号</th><td>R2026-11-11-01</td></tr>
<tr><th>标段号</th><td>SG-01</td></tr>
<tr><th>标段名称</th><td>办公设备采购</td></tr>
</table>
<table>
<tr><th>序号</th><th>统一社会信用代码</th><th>投标企业</th><th>唱标报价（元）</th></tr>
{rows}
</table>
{pager}
</body></html>
"""

    def _send_html(self, body: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        if self.path == "/records/B2026-001/detail/SG-01.html":
            rows = (
                "<tr><td>1</td><td>91330100MA2A0001X1</td><td>杭州启明办公设备有限公司</td><td>1,200,000.00</td></tr>\n"
                "<tr><td>2</td><td>91330100MA2A0002X2</td><td>浙江远信科技有限公司</td><td>1,180,000.00</td></tr>"
            )
            self._send_html(self.PAGE_TEMPLATE.format(
                rows=rows, pager='<div class="pager"><a href="SG-01_p2.html">下一页</a></div>'
            ))
            return
        if self.path == "/records/B2026-001/detail/SG-01_p2.html":
            rows = (
                "<tr><td>3</td><td>91330100MA2A0003X3</td><td>宁波华辰商贸有限公司</td><td>1,215,000.00</td></tr>"
            )
            self._send_html(self.PAGE_TEMPLATE.format(
                rows=rows, pager='<div class="pager"><span class="disabled">下一页</span></div>'
            ))
            return
        super().do_GET()


class RevisionChangingHandler(QuietHandler):
    """第一次读取目录首页正常，结束前复核时返回改动的修订号。"""

    def do_GET(self) -> None:
        if self.path == "/records/B2026-001/index.html":
            self.server.home_reads += 1
            if self.server.home_reads >= 2:
                html = (SITE_DIR / "records/B2026-001/index.html").read_text(encoding="utf-8")
                changed = html.replace("R2026-11-11-01", "R2026-11-11-01-CHANGED", 1)
                body = changed.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
        super().do_GET()


def start_server(handler_cls=QuietHandler) -> tuple[http.server.ThreadingHTTPServer, str]:
    handler = functools.partial(handler_cls, directory=str(SITE_DIR))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.home_reads = 0  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def assert_equal(actual, expected, message: str) -> None:
    if actual != expected:
        raise AssertionError(f"{message}：期望 {expected!r}，实际 {actual!r}")
    print(f"  ✓ {message}")


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"  ✓ {message}")


def check_consistent(base: str) -> None:
    report = verify_batch(f"{base}/records/B2026-001/index.html", "B2026-001")
    assert_equal(report.status, BatchStatus.CONSISTENT, "B2026-001 判定为一致")
    assert_equal(report.problems, [], "B2026-001 无问题")
    assert_equal(len(report.directory_pages), 2, "B2026-001 沿下一页读取 2 个目录页")
    assert_equal(len(report.sections), 3, "B2026-001 读到 3 个标段")
    by_no = {section.section_no: section for section in report.sections}
    assert_equal(by_no["SG-01"].detail_count, 3, "SG-01 明细去重后 3 家")
    assert_equal(by_no["SG-02"].detail_count, 2, "SG-02 同一企业同报价重复记录去重为 2 家")
    assert_equal(by_no["SG-02"].summary_count, 2, "SG-02 汇总人数 2 家")
    for section in report.sections:
        assert_equal(section.status, "一致", f"{section.section_no} 人数/最低报价一致")
        assert_equal(section.count_diff, 0, f"{section.section_no} 人数差异为 0")


def check_difference(base: str) -> None:
    report = verify_batch(f"{base}/records/B2026-002/index.html", "B2026-002")
    assert_equal(report.status, BatchStatus.DIFFERENCE, "B2026-002 判定为存在差异")
    by_no = {section.section_no: section for section in report.sections}
    sg01 = by_no["SG-01"]
    assert_equal(sg01.summary_count, 2, "SG-01 汇总记 2 家")
    assert_equal(sg01.detail_count, 3, "SG-01 明细去重 3 家")
    assert_equal(sg01.count_diff, 1, "SG-01 明细比汇总多 1 家（漏记）")
    assert_equal(str(sg01.summary_min), "760000.00", "SG-01 汇总最低报价 760000")
    assert_equal(str(sg01.detail_min), "720000.00", "SG-01 明细最低报价 720000")
    assert_equal(str(sg01.min_diff), "-40000.00", "SG-01 最低报价差异 -40000")
    assert_equal(sg01.status, "存在差异", "SG-01 标记差异")
    assert_equal(by_no["SG-02"].status, "一致", "SG-02 一致")


def check_conflict(base: str) -> None:
    report = verify_batch(f"{base}/records/B2026-003/index.html", "B2026-003")
    assert_equal(report.status, BatchStatus.CONFLICT, "B2026-003 判定为存在冲突")
    assert_equal(report.problems, [], "B2026-003 无读取/修订问题")
    assert_equal(len(report.conflicts), 1, "识别 1 个报价冲突")
    conflict = report.conflicts[0]
    assert_equal(conflict.section_no, "SG-01", "冲突位于 SG-01")
    assert_equal(conflict.credit_code, "91330100MA2C0001Z1", "冲突企业信用代码正确")
    assert_equal(len(conflict.occurrences), 2, "冲突保留两条不同报价记录")
    values = sorted(str(item.quote) for item in conflict.occurrences)
    assert_equal(values, ["1020000.00", "1050000.00"], "两条冲突报价均保留，未任选其一")
    by_no = {section.section_no: section for section in report.sections}
    assert_equal(by_no["SG-01"].detail_min, None, "冲突标段不核算最低报价")
    assert_equal(by_no["SG-01"].status, "报价冲突", "冲突标段状态为报价冲突")
    assert_equal(by_no["SG-02"].status, "一致", "无冲突标段仍为一致")


def check_unable(base: str, batch: str, expected_fragment: str) -> None:
    report = verify_batch(f"{base}/records/{batch}/index.html", batch)
    assert_equal(report.status, BatchStatus.UNABLE, f"{batch} 判定为无法核定")
    assert_true(bool(report.problems), f"{batch} 给出问题列表")
    assert_true(
        any(expected_fragment in problem.reason or expected_fragment in problem.url for problem in report.problems),
        f"{batch} 问题中包含 {expected_fragment}",
    )
    assert_true(
        all(section.count_diff is None and section.min_diff is None for section in report.sections),
        f"{batch} 未比较人数与最低报价",
    )
    assert_true(
        all(problem.url for problem in report.problems),
        f"{batch} 每个问题都带来源链接",
    )
    return report


def check_revision_change_on_recheck(base: str) -> None:
    report = verify_batch(f"{base}/records/B2026-001/index.html", "B2026-001")
    assert_equal(report.status, BatchStatus.UNABLE, "复核时修订号变化 -> 无法核定")
    assert_true(
        any(problem.kind == "目录复核" and "变化" in problem.reason for problem in report.problems),
        "问题列表记录目录首页修订号变化",
    )
    assert_true(
        any(problem.url.endswith("/records/B2026-001/index.html") for problem in report.problems),
        "目录复核问题带目录首页链接",
    )


def check_detail_pagination(base: str) -> None:
    report = verify_batch(f"{base}/records/B2026-001/index.html", "B2026-001")
    assert_equal(report.status, BatchStatus.CONSISTENT, "明细分页合并后仍判定为一致")
    assert_equal(report.problems, [], "明细分页无问题")
    assert_true(
        any(page.endswith("/detail/SG-01_p2.html") for page in report.detail_pages),
        "读取了明细页的“下一页”",
    )
    by_no = {section.section_no: section for section in report.sections}
    assert_equal(by_no["SG-01"].detail_count, 3, "两页明细合并去重后为 3 家")
    assert_equal(str(by_no["SG-01"].detail_min), "1180000.00", "跨页明细最低报价正确")


def main() -> int:
    generate_site_main()
    server, base = start_server()
    print(f"[1] 本地示例站点：{base}")

    print("[2] 一致批次：修订一致、抓取完整、人数与最低报价匹配 ...")
    check_consistent(base)

    print("[3] 批次选择页：从开标记录目录按批次号定位到批次目录 ...")
    resolved = verify_batch(f"{base}/records/index.html", "B2026-001")
    assert_equal(resolved.status, BatchStatus.CONSISTENT, "从批次选择页输入批次号后判定为一致")
    assert_true(
        resolved.directory_home.endswith("/records/B2026-001/index.html"),
        "按批次号定位到 B2026-001 批次目录",
    )
    missing_batch = verify_batch(f"{base}/records/index.html", "B2026-999")
    assert_equal(missing_batch.status, BatchStatus.UNABLE, "批次选择页中不存在的批次号 -> 无法核定")
    assert_true(
        any("未找到与批次号" in problem.reason for problem in missing_batch.problems),
        "问题列表提示未找到匹配的批次目录链接",
    )

    print("[4] 差异批次：汇总漏记投标人且最低报价不一致 ...")
    check_difference(base)

    print("[5] 冲突批次：同一企业不同报价，不任选其一核算 ...")
    check_conflict(base)

    print("[6] 无法核定：修订号不一致 / 明细页读取失败 / 触及抓取上限 ...")
    check_unable(base, "B2026-004", "修订号与目录首页不一致")
    check_unable(base, "B2026-005", "detail/SG-02.html")
    limit_report = check_unable(base, "B2026-006", "达到目录页抓取上限")
    assert_equal(len(limit_report.directory_pages), 10, "B2026-006 只读取到 10 个目录页即触发上限")

    print("[7] 批次号不匹配 -> 无法核定 ...")
    mismatch = verify_batch(f"{base}/records/B2026-001/index.html", "B2026-999")
    assert_equal(mismatch.status, BatchStatus.UNABLE, "批次号不匹配判定为无法核定")
    assert_true(
        any("与输入批次号" in problem.reason for problem in mismatch.problems),
        "问题列表记录批次号不匹配",
    )

    print("[8] 结束前重读目录首页，修订变化 -> 无法核定 ...")
    server2, base2 = start_server(RevisionChangingHandler)
    check_revision_change_on_recheck(base2)

    print("[9] 明细分页：沿“下一页”合并两页唱标记录 ...")
    server3, base3 = start_server(DetailPagingHandler)
    check_detail_pagination(base3)

    print("[10] 解析细节：标段号误匹配与万元报价 ...")
    html = (SITE_DIR / "records" / "B2026-006" / "index.html").read_text(encoding="utf-8")
    parsed = parse_directory_page(html, f"{base}/records/B2026-006/index.html")
    assert_equal(parsed.entries[0].section_no, "SG-01", "标段名称含“标段 1”时不误判标段号")
    assert_equal(str(_parse_money("118.00万元")), "1180000.00", "万元报价规范为元")
    assert_equal(str(_parse_money("1,180,000.00 元")), "1180000.00", "带千分位的报价规范为元")
    assert_equal(_parse_money("详见附件"), None, "非金额文本不被误解析")

    print("\n全部开标批次核对测试通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
