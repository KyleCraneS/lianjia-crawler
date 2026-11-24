# -*- coding: utf-8 -*-
"""把详情 CSV 转成格式化 Excel：JSON 字段展开成独立列，冻结表头+筛选+列宽。"""
import csv
import glob
import json
import os

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '输出')
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '输出', '链家二手房_样本.xlsx')

COLS = [
    ('房源ID', 12), ('标题', 36), ('卖点描述', 30),
    ('总价_万', 9), ('单价_元每平', 11),
    ('户型', 9), ('楼层', 13), ('朝向', 8), ('结构_装修', 10),
    ('建筑面积', 11), ('年代_楼型', 13), ('套内面积', 9), ('梯户比例', 9), ('配备电梯', 8),
    ('标签', 12),
    ('小区名称', 13), ('小区ID', 15), ('区域_区', 9), ('区域_商圈', 11), ('区域_环线', 9),
    ('地铁', 17), ('看房时间', 15),
    ('链家编号', 14), ('挂牌时间', 12), ('上次交易', 12), ('房屋年限', 9),
    ('交易权属', 9), ('房屋用途', 9), ('产权所属', 8), ('抵押信息', 9),
    ('核心卖点', 36), ('周边配套', 36), ('税费解析', 30), ('装修描述', 30), ('交通出行', 30),
    ('经纪人门店', 20), ('经纪人机构', 22), ('经纪人电话', 17), ('经纪人司龄', 10), ('经纪人学校', 15),
    ('经度', 13), ('纬度', 13), ('抓取时间', 18), ('状态', 8), ('url', 40),
]


def main():
    # 合并 输出/ 下所有详情 CSV（单会话 details.csv + 调度器的 details_A/B/C.csv + 别的电脑拷来的），按链接去重
    files = sorted(glob.glob(os.path.join(OUT_DIR, '*details*.csv')))
    if not files:
        print(f'没找到详情 CSV：{OUT_DIR} 下的 *details*.csv')
        return
    rows, seen = [], set()
    for fp in files:
        for r in csv.DictReader(open(fp, encoding='utf-8-sig')):
            key = r.get('url') or r.get('房源ID') or r.get('链家编号')
            if key in seen:
                continue
            seen.add(key)
            rows.append(r)
    print(f'读取 {len(files)} 个文件，去重后 {len(rows)} 条')
    wb = Workbook()
    ws = wb.active
    ws.title = '链家二手房'

    header_font = Font(name='Arial', bold=True, color='FFFFFF', size=10)
    header_fill = PatternFill('solid', fgColor='4472C4')
    body_font = Font(name='Arial', size=10)
    thin = Side(style='thin', color='D9D9D9')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    wrap = Alignment(wrap_text=True, vertical='top')
    center = Alignment(horizontal='center', vertical='center', wrap_text=True)

    for ci, (name, width) in enumerate(COLS, 1):
        c = ws.cell(1, ci, name)
        c.font = header_font
        c.fill = header_fill
        c.alignment = center
        c.border = border
        ws.column_dimensions[get_column_letter(ci)].width = width
    ws.row_dimensions[1].height = 24

    for r in rows:
        intro = {}
        try:
            intro = json.loads(r.get('房源介绍') or '{}')
        except json.JSONDecodeError:
            pass
        vals = []
        for name, _ in COLS:
            if name == '周边配套':
                vals.append(intro.get('周边配套', ''))
            elif name == '税费解析':
                vals.append(intro.get('税费解析', ''))
            elif name == '装修描述':
                vals.append(intro.get('装修描述', ''))
            elif name == '交通出行':
                vals.append(intro.get('交通出行', ''))
            else:
                vals.append(r.get(name, ''))
        # 防公式注入：采集来的文本若以 = + - @ 开头，Excel 会当公式执行，前面加单引号转成纯文本
        vals = ["'" + v if isinstance(v, str) and v[:1] in ('=', '+', '-', '@', '\t', '\r') else v
                for v in vals]
        ws.append(vals)

    for row in ws.iter_rows(min_row=2, max_row=ws.max_row, max_col=len(COLS)):
        for c in row:
            c.font = body_font
            c.border = border
            if c.column_letter in ('B', 'C', 'AE', 'AF', 'AG', 'AH', 'AI', 'AO'):
                c.alignment = wrap

    ws.freeze_panes = 'C2'
    ws.auto_filter.ref = f'A1:{get_column_letter(len(COLS))}{ws.max_row}'
    wb.save(OUT)
    print(f'完成: {len(rows)} 条 -> {OUT}')


if __name__ == '__main__':
    main()
