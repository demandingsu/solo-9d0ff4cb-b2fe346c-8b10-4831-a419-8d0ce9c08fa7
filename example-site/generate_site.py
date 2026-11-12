"""生成本地示例招标站点（纯静态 HTML，构建时运行一次）。

覆盖的业务场景：
  P1 更正公告给出新截止时间        -> 有效，截止时间被替换
  P2 延期公告给出新截止时间        -> 有效，截止时间被替换
  P3 延期公告声明延期但未给新时间  -> 待核实，旧时间不再显示
  P4 两次更正：先改时间、后延期无时间 -> 待核实
  P5 只有原始招标公告              -> 有效，沿用原截止时间
  P6 公告未公布任何截止时间        -> 未公布
列表页分两页，通过“下一页”链接串联，验证分页抓取。
另生成 6 个开标批次记录场景（records/B2026-001 ~ B2026-006），覆盖一致、漏记差异、
报价冲突、修订号不一致、明细页读取失败、目录页抓取上限。
"""
from __future__ import annotations

import html
import random
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

OUTPUT = Path(__file__).resolve().parent / "site"

# (项目编号, 项目名称, 公告类型, 发布时间, 截止时间或 None, 标题, 正文补充)
NOTICES: list[tuple[str, str, str, str, str | None, str, str]] = [
    # --- P1：更正公告写明新截止时间 ---
    ("CG-2026-001", "市政道路养护设备采购", "招标公告", "2026-10-08 09:00:00",
     "2026-10-30 14:30:00", "市政道路养护设备采购招标公告",
     "欢迎符合条件的供应商参加投标。"),
    ("CG-2026-001", "市政道路养护设备采购", "更正公告", "2026-10-15 16:20:00",
     "2026-11-06 10:00:00", "市政道路养护设备采购更正公告",
     "因采购需求调整，现将投标截止时间延期至更正公告载明的新时间，请各投标人知悉。"),

    # --- P2：延期公告写明新截止时间 ---
    ("CG-2026-002", "校园智慧图书馆系统采购", "招标公告", "2026-09-25 10:00:00",
     "2026-10-20 09:30:00", "校园智慧图书馆系统采购项目招标公告",
     "本项目采用公开招标方式，详见招标文件。"),
    ("CG-2026-002", "校园智慧图书馆系统采购", "延期公告", "2026-10-16 11:00:00",
     "2026-10-28 15:00:00", "校园智慧图书馆系统采购项目延期公告",
     "因接到答疑申请，投标截止时间顺延至本公告载明的新时间，开标时间同步调整。"),

    # --- P3：延期公告声明延期，但没有给出新截止时间 ---
    ("CG-2026-003", "医院影像设备维保服务采购", "招标公告", "2026-10-02 08:45:00",
     "2026-10-25 09:00:00", "医院影像设备维保服务采购招标公告",
     "投标人须在截止时间前完成电子投标文件递交。"),
    ("CG-2026-003", "医院影像设备维保服务采购", "延期公告", "2026-10-21 17:30:00",
     None, "医院影像设备维保服务采购延期公告",
     "因评审安排冲突，本项目投标截止时间需要延期，具体新的投标截止时间将另行公告通知。"),

    # --- P4：两次更正，先给新时间、后只声明延期 ---
    ("CG-2026-004", "政务云平台扩容采购", "招标公告", "2026-09-20 09:00:00",
     "2026-10-12 14:00:00", "政务云平台扩容采购招标公告",
     "本项目不接受联合体投标。"),
    ("CG-2026-004", "政务云平台扩容采购", "更正公告", "2026-10-01 15:10:00",
     "2026-10-22 14:00:00", "政务云平台扩容采购更正公告",
     "现对招标文件部分参数进行更正，投标截止时间相应延期至新时间。"),
    ("CG-2026-004", "政务云平台扩容采购", "延期公告", "2026-10-19 10:05:00",
     None, "政务云平台扩容采购延期公告",
     "因上级系统升级，开标场地无法按时使用，本项目投标及开标将顺延，新时间另行通知。"),

    # --- P5：仅有原始招标公告 ---
    ("CG-2026-005", "公办幼儿园教具采购", "招标公告", "2026-10-10 13:30:00",
     "2026-11-02 09:30:00", "公办幼儿园教具采购招标公告",
     "欢迎合格供应商参与投标。"),

    # --- P6：公告未公布任何投标截止时间 ---
    ("CG-2026-006", "档案馆数字化加工服务采购", "招标公告", "2026-10-12 10:40:00",
     None, "档案馆数字化加工服务采购招标公告",
     "本项目采用竞争性方式采购，具体投标安排详见后续通知。"),
]

FIELD_LABELS = [
    ("project_no", "项目编号"),
    ("notice_type", "公告类型"),
    ("publish_time", "发布时间"),
    ("deadline", "投标截止时间"),
]


def detail_filename(index: int) -> str:
    return f"notice_{index:03d}.html"


def build_detail(notice: tuple, index: int) -> str:
    project_no, project_name, notice_type, publish_time, deadline, title, body = notice
    rng = random.Random(index)

    # 打乱字段顺序，模拟不同页面模板；无新时间的延期页不输出截止时间字段
    labels = [item for item in FIELD_LABELS if not (item[0] == "deadline" and deadline is None)]
    rng.shuffle(labels)

    rows = [f"<tr><th>项目名称</th><td>{html.escape(project_name)}</td></tr>"]
    for key, label in labels:
        value = {
            "project_no": project_no,
            "notice_type": notice_type,
            "publish_time": publish_time,
            "deadline": deadline,
        }[key]
        rows.append(f"<tr><th>{label}</th><td>{html.escape(str(value))}</td></tr>")
    rng.shuffle(rows)
    table = "\n      ".join(rows)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{html.escape(title)}</title>
<link rel="stylesheet" href="../assets/style.css">
</head>
<body>
<div class="wrap">
  <p class="crumb"><a href="../list/page_1.html">返回公告列表</a></p>
  <h1>{html.escape(title)}</h1>
  <table class="fields">
      {table}
  </table>
  <div class="content">
    <p>{html.escape(body)}</p>
    <p>采购代理人请以本平台正式公告内容为准，按时完成投标文件递交。</p>
  </div>
</div>
</body>
</html>
"""


def build_list_page(page_no: int, page_items: list[tuple], has_next: bool) -> str:
    items_html = []
    for index, notice in page_items:
        title = notice[5]
        publish_time = notice[3]
        href = f"../detail/{detail_filename(index)}"
        items_html.append(
            f'    <li><a class="notice-link" href="{href}">'
            f'<span class="t">{html.escape(title)}</span>'
            f'<span class="d">{publish_time}</span></a></li>'
        )
    pager = (
        '<div class="pager">'
        f'<a class="next-page" href="page_{page_no + 1}.html">下一页</a>'
        "</div>"
        if has_next
        else '<div class="pager"><span class="disabled">下一页</span></div>'
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>招标采购公告列表 - 第{page_no}页</title>
<link rel="stylesheet" href="../assets/style.css">
</head>
<body>
<div class="wrap">
  <h1>招标采购公告列表</h1>
  <ul class="notice-list">
{chr(10).join(items_html)}
  </ul>
{pager}
</div>
</body>
</html>
"""


def build_index() -> str:
    return """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>示例采购信息平台</title>
<link rel="stylesheet" href="assets/style.css">
</head>
<body>
<div class="wrap">
  <h1>示例采购信息平台</h1>
  <p><a href="list/page_1.html">进入招标采购公告列表</a></p>
  <p><a href="records/index.html">进入开标批次记录目录</a></p>
</div>
</body>
</html>
"""


CSS = """
body { margin:0; background:#f5f7fa; font-family:"PingFang SC","Microsoft YaHei",sans-serif; color:#1f2937; }
.wrap { max-width:960px; margin:24px auto; background:#fff; padding:28px 36px; border-radius:10px; box-shadow:0 1px 4px rgba(0,0,0,.08); }
h1 { font-size:20px; margin:0 0 18px; }
.crumb a { color:#1d4ed8; text-decoration:none; font-size:13px; }
table.fields { border-collapse:collapse; width:100%; margin:16px 0; }
table.fields th, table.fields td { border:1px solid #d7dde5; padding:9px 12px; font-size:14px; text-align:left; }
table.fields th { background:#f3f6fb; width:160px; white-space:nowrap; }
.content { line-height:1.8; font-size:14px; }
ul.notice-list { list-style:none; margin:0; padding:0; }
ul.notice-list li { border-bottom:1px solid #eef1f5; }
.notice-link { display:flex; justify-content:space-between; padding:13px 6px; text-decoration:none; color:#1f2937; }
.notice-link:hover { background:#f8fafc; }
.notice-link .t { font-size:14px; }
.notice-link .d { color:#64748b; font-size:13px; white-space:nowrap; margin-left:20px; }
.pager { margin-top:20px; text-align:right; }
.pager a, .pager .disabled { display:inline-block; padding:7px 16px; border:1px solid #cbd5e1; border-radius:6px; font-size:13px; }
.pager a { background:#1d4ed8; color:#fff; text-decoration:none; border-color:#1d4ed8; }
.pager .disabled { color:#94a3b8; background:#f1f5f9; }
"""


# ---------------------------------------------------------------------------
# 开标批次记录示例（目录 -> 唱标明细 -> 人数/最低报价汇总）
# ---------------------------------------------------------------------------


@dataclass
class SectionSpec:
    no: str
    name: str
    bidders: list[tuple[str, str, str]]
    summary_count: int | None = None
    summary_min: str | None = None
    detail_revision: str | None = None
    summary_revision: str | None = None
    missing_detail: bool = False


@dataclass
class BatchSpec:
    no: str
    revision: str
    plan: str
    sections: list[SectionSpec]
    pages: int = 2


RECORD_BATCHES: list[BatchSpec] = [
    BatchSpec(
        no="B2026-001",
        revision="R2026-11-11-01",
        plan="核对一致：目录、明细、汇总修订号相同，人数与最低报价均匹配。",
        pages=2,
        sections=[
            SectionSpec("SG-01", "办公设备采购", [
                ("91330100MA2A0001X1", "杭州启明办公设备有限公司", "1,200,000.00"),
                ("91330100MA2A0002X2", "浙江远信科技有限公司", "1,180,000.00"),
                ("91330100MA2A0003X3", "宁波华辰商贸有限公司", "1,215,000.00"),
            ]),
            SectionSpec("SG-02", "网络设备采购", [
                ("91330100MA2A0004X4", "杭州联创网络技术有限公司", "980,000.00"),
                ("91330100MA2A0005X5", "温州智联信息工程有限公司", "995,000.00"),
                # 同一企业同一报价重复出现：应按标段号 + 统一社会信用代码去重，不应重复计数
                ("91330100MA2A0005X5", "温州智联信息工程有限公司", "995,000.00"),
            ]),
            SectionSpec("SG-03", "运维服务采购", [
                ("91330100MA2A0006X6", "嘉兴恒信运维服务有限公司", "450,000.00"),
            ]),
        ],
    ),
    BatchSpec(
        no="B2026-002",
        revision="R2026-11-11-02",
        plan="汇总漏记：SG-01 明细有 3 家、汇总只记 2 家，且汇总最低报价高于明细最低报价。",
        pages=2,
        sections=[
            SectionSpec(
                "SG-01",
                "绿化养护服务采购",
                [
                    ("91330100MA2B0001Y1", "杭州青禾园林有限公司", "800,000.00"),
                    ("91330100MA2B0002Y2", "绍兴景园绿化有限公司", "760,000.00"),
                    ("91330100MA2B0003Y3", "湖州绿地养护有限公司", "720,000.00"),
                ],
                summary_count=2,
                summary_min="760,000.00",
            ),
            SectionSpec("SG-02", "保安服务采购", [
                ("91330100MA2B0004Y4", "杭州安泰保安服务有限公司", "600,000.00"),
                ("91330100MA2B0005Y5", "宁波甬盾保安服务有限公司", "620,000.00"),
            ]),
        ],
    ),
    BatchSpec(
        no="B2026-003",
        revision="R2026-11-11-03",
        plan="报价冲突：SG-01 同一统一社会信用代码出现两个不同报价，冲突标段不核算最低报价。",
        pages=2,
        sections=[
            SectionSpec(
                "SG-01",
                "食堂食材配送服务",
                [
                    ("91330100MA2C0001Z1", "杭州绿膳餐饮管理有限公司", "1,050,000.00"),
                    ("91330100MA2C0001Z1", "杭州绿膳餐饮管理有限公司", "1,020,000.00"),
                    ("91330100MA2C0002Z2", "宁波鲜丰供应链有限公司", "1,100,000.00"),
                ],
                summary_count=2,
                summary_min="1,020,000.00",
            ),
            SectionSpec("SG-02", "办公家具采购", [
                ("91330100MA2C0003Z3", "杭州优派家具有限公司", "860,000.00"),
                ("91330100MA2C0004Z4", "嘉兴华庭家具有限公司", "880,000.00"),
            ]),
        ],
    ),
    BatchSpec(
        no="B2026-004",
        revision="R2026-11-11-04",
        plan="修订号不一致：SG-01 的汇总页修订号为 R2026-11-11-04-B，与目录/明细不同。",
        pages=2,
        sections=[
            SectionSpec(
                "SG-01",
                "物业服务采购",
                [
                    ("91330100MA2D0001W1", "杭州滨江物业管理有限公司", "1,300,000.00"),
                    ("91330100MA2D0002W2", "浙江大家物业服务有限公司", "1,280,000.00"),
                ],
                summary_revision="R2026-11-11-04-B",
            ),
            SectionSpec("SG-02", "绿化养护采购", [
                ("91330100MA2D0003W3", "杭州园林绿化工程有限公司", "700,000.00"),
                ("91330100MA2D0004W4", "绍兴越都园林有限公司", "680,000.00"),
            ]),
        ],
    ),
    BatchSpec(
        no="B2026-005",
        revision="R2026-11-11-05",
        plan="页面读取失败：SG-02 的唱标明细链接存在，但明细页缺失（404）。",
        pages=2,
        sections=[
            SectionSpec("SG-01", "消防维保服务", [
                ("91330100MA2E0001V1", "杭州消防设备维保有限公司", "500,000.00"),
                ("91330100MA2E0002V2", "浙江安盾消防技术有限公司", "480,000.00"),
            ]),
            SectionSpec(
                "SG-02",
                "电梯维保服务",
                [
                    ("91330100MA2E0003V3", "杭州西奥电梯服务有限公司", "300,000.00"),
                    ("91330100MA2E0004V4", "宁波宏大电梯工程有限公司", "310,000.00"),
                ],
                missing_detail=True,
            ),
        ],
    ),
    BatchSpec(
        no="B2026-006",
        revision="R2026-11-11-06",
        plan="触及抓取上限：目录共 12 页，超过目录页上限 10 页，无法确认已完整遍历。",
        pages=12,
        sections=[
            SectionSpec(
                f"SG-{index:02d}",
                f"上限测试标段 {index}",
                [(f"91330100MA2F{index:04d}U{index % 10}", f"测试投标企业 {index}", f"{500000 + index * 1000:,.2f}")],
            )
            for index in range(1, 13)
        ],
    ),
]

RECORD_FIELD_LABELS = [
    ("batch_no", "批次号"),
    ("revision", "批次修订号"),
    ("section_no", "标段号"),
    ("section_name", "标段名称"),
]


def _quote_value(text: str) -> Decimal:
    return Decimal(text.replace(",", ""))


def _summary_values(section: SectionSpec) -> tuple[int, str]:
    count = section.summary_count
    if count is None:
        count = len({bidder[0] for bidder in section.bidders})
    if section.summary_min is not None:
        min_text = section.summary_min
    else:
        values = [_quote_value(bidder[2]) for bidder in section.bidders]
        min_text = f"{min(values):,.2f}"
    return count, min_text


def _split_sections(sections: list[SectionSpec], pages: int) -> list[list[SectionSpec]]:
    pages = max(1, min(pages, len(sections)))
    per_page = (len(sections) + pages - 1) // pages
    return [sections[index:index + per_page] for index in range(0, len(sections), per_page)]


def _record_meta(batch: BatchSpec, revision: str, section: SectionSpec | None = None) -> str:
    rows = [
        f"<tr><th>批次号</th><td>{html.escape(batch.no)}</td></tr>",
        f"<tr><th>批次修订号</th><td>{html.escape(revision)}</td></tr>",
    ]
    if section is not None:
        rows.append(f"<tr><th>标段号</th><td>{html.escape(section.no)}</td></tr>")
        rows.append(f"<tr><th>标段名称</th><td>{html.escape(section.name)}</td></tr>")
    return "\n      ".join(rows)


def build_record_directory_page(
    batch: BatchSpec, page_no: int, total_pages: int, page_sections: list[SectionSpec]
) -> str:
    rows = []
    for section in page_sections:
        rows.append(
            "<tr>"
            f"<td>{html.escape(section.no)}</td>"
            f"<td>{html.escape(section.name)}</td>"
            f'<td><a href="detail/{html.escape(section.no)}.html">唱标明细</a></td>'
            f'<td><a href="summary/{html.escape(section.no)}.html">人数与最低报价汇总</a></td>'
            "</tr>"
        )
    pager = (
        '<div class="pager"><a href="page_%d.html">下一页</a></div>' % (page_no + 1)
        if page_no < total_pages
        else '<div class="pager"><span class="disabled">下一页</span></div>'
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>开标批次记录目录 - {html.escape(batch.no)} - 第{page_no}页</title>
<link rel="stylesheet" href="../../assets/style.css">
</head>
<body>
<div class="wrap">
  <p class="crumb"><a href="../index.html">返回开标记录目录</a></p>
  <h1>开标批次记录目录 - {html.escape(batch.no)}</h1>
  <table class="fields">
      {_record_meta(batch, batch.revision)}
      <tr><th>目录页</th><td>第 {page_no} / {total_pages} 页</td></tr>
  </table>
  <p class="content">{html.escape(batch.plan)}</p>
  <table class="fields records">
    <tr><th>标段号</th><th>标段名称</th><th>唱标明细</th><th>人数/最低报价汇总</th></tr>
    {chr(10).join("    " + row for row in rows)}
  </table>
  {pager}
</div>
</body>
</html>
"""


def build_bid_detail_page(batch: BatchSpec, section: SectionSpec) -> str:
    revision = section.detail_revision or batch.revision
    rows = []
    for index, (code, name, quote) in enumerate(section.bidders, start=1):
        rows.append(
            f"<tr><td>{index}</td><td>{html.escape(code)}</td>"
            f"<td>{html.escape(name)}</td><td>{html.escape(quote)}</td></tr>"
        )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>标段 {html.escape(section.no)} 唱标明细 - {html.escape(batch.no)}</title>
<link rel="stylesheet" href="../../../assets/style.css">
</head>
<body>
<div class="wrap">
  <p class="crumb"><a href="../index.html">返回开标批次记录目录</a></p>
  <h1>标段 {html.escape(section.no)} 企业唱标明细</h1>
  <table class="fields">
      {_record_meta(batch, revision, section)}
      <tr><th>统计口径</th><td>按统一社会信用代码去重；同一企业报价不同应标记冲突</td></tr>
  </table>
  <table class="fields bids">
    <tr><th>序号</th><th>统一社会信用代码</th><th>投标企业</th><th>唱标报价（元）</th></tr>
    {chr(10).join("    " + row for row in rows)}
  </table>
</div>
</body>
</html>
"""


def build_summary_page(batch: BatchSpec, section: SectionSpec) -> str:
    revision = section.summary_revision or batch.revision
    count, min_text = _summary_values(section)
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>标段 {html.escape(section.no)} 人数与最低报价汇总 - {html.escape(batch.no)}</title>
<link rel="stylesheet" href="../../../assets/style.css">
</head>
<body>
<div class="wrap">
  <p class="crumb"><a href="../index.html">返回开标批次记录目录</a></p>
  <h1>标段 {html.escape(section.no)} 唱标汇总</h1>
  <table class="fields">
      {_record_meta(batch, revision, section)}
      <tr><th>投标人数</th><td>{count} 家</td></tr>
      <tr><th>最低唱标报价</th><td>{html.escape(min_text)} 元</td></tr>
  </table>
  <p class="content">本汇总按统一社会信用代码去重统计；人数或最低报价与明细不一致时，应回查唱标记录并决定是否补录。</p>
</div>
</body>
</html>
"""


def build_records_index(batches: list[BatchSpec]) -> str:
    rows = []
    for batch in batches:
        rows.append(
            f'<tr><td><a href="{html.escape(batch.no)}/index.html">{html.escape(batch.no)}</a></td>'
            f"<td>{html.escape(batch.revision)}</td>"
            f"<td>{len(batch.sections)}</td>"
            f"<td>{html.escape(batch.plan)}</td></tr>"
        )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>开标记录目录</title>
<link rel="stylesheet" href="../assets/style.css">
</head>
<body>
<div class="wrap">
  <p class="crumb"><a href="../index.html">返回示例采购信息平台</a></p>
  <h1>开标记录目录（示例）</h1>
  <p class="content">选择批次进入记录目录首页，再把该页地址与批次号填入核对页面。各批次覆盖一致、漏记、冲突、修订不一致、读取失败和抓取上限等场景。</p>
  <table class="fields">
    <tr><th>批次号</th><th>批次修订号</th><th>标段数</th><th>场景说明</th></tr>
    {chr(10).join("    " + row for row in rows)}
  </table>
</div>
</body>
</html>
"""


def generate_records(output: Path) -> None:
    records_root = output / "records"
    records_root.mkdir(parents=True, exist_ok=True)
    (records_root / "index.html").write_text(build_records_index(RECORD_BATCHES), encoding="utf-8")

    for batch in RECORD_BATCHES:
        batch_dir = records_root / batch.no
        (batch_dir / "detail").mkdir(parents=True, exist_ok=True)
        (batch_dir / "summary").mkdir(parents=True, exist_ok=True)

        page_groups = _split_sections(batch.sections, batch.pages)
        total_pages = len(page_groups)
        for page_no, page_sections in enumerate(page_groups, start=1):
            filename = "index.html" if page_no == 1 else f"page_{page_no}.html"
            (batch_dir / filename).write_text(
                build_record_directory_page(batch, page_no, total_pages, page_sections),
                encoding="utf-8",
            )

        for section in batch.sections:
            if not section.missing_detail:
                (batch_dir / "detail" / f"{section.no}.html").write_text(
                    build_bid_detail_page(batch, section), encoding="utf-8"
                )
            (batch_dir / "summary" / f"{section.no}.html").write_text(
                build_summary_page(batch, section), encoding="utf-8"
            )


def main() -> None:
    (OUTPUT / "list").mkdir(parents=True, exist_ok=True)
    (OUTPUT / "detail").mkdir(parents=True, exist_ok=True)
    (OUTPUT / "assets").mkdir(parents=True, exist_ok=True)

    indexed = list(enumerate(NOTICES, start=1))
    # 列表页按发布时间倒序（最新在前），分两页
    ordered = sorted(indexed, key=lambda pair: pair[1][3], reverse=True)
    half = (len(ordered) + 1) // 2
    pages = [ordered[:half], ordered[half:]]

    for page_no, page_items in enumerate(pages, start=1):
        has_next = page_no < len(pages)
        (OUTPUT / "list" / f"page_{page_no}.html").write_text(
            build_list_page(page_no, page_items, has_next), encoding="utf-8"
        )

    for index, notice in indexed:
        (OUTPUT / "detail" / detail_filename(index)).write_text(
            build_detail(notice, index), encoding="utf-8"
        )

    (OUTPUT / "index.html").write_text(build_index(), encoding="utf-8")
    (OUTPUT / "assets" / "style.css").write_text(CSS, encoding="utf-8")
    generate_records(OUTPUT)
    print(f"示例站点已生成：{OUTPUT}（{len(NOTICES)} 条公告，{len(pages)} 个列表页，{len(RECORD_BATCHES)} 个开标批次）")


if __name__ == "__main__":
    main()
