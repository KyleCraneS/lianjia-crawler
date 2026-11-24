# lianjia-crawler · 链家二手房采集工具

链家二手房全量采集（城市可配置）：sitemap 拿全量房源链接 → 逐套抓详情页（46 个字段）→ 导出 Excel。
自带账号池轮换、请求调速、断点续传、崩溃自愈，以及极验 v4 点选/语序验证码的本地 ONNX 识别模型（约 40MB，已包含在仓库里）。

> ⚠️ **免责声明**：本项目仅供学习和技术研究。使用前请阅读并遵守链家网站的用户协议和 robots 规则，以及当地法律法规；
> 请控制采集频率，不要对网站造成压力，采集的数据不得用于商业用途或公开传播。因使用本项目产生的一切后果由使用者自行承担。

## 目录结构

```
lianjia-crawler/
├── scripts/                   全部代码
│   ├── lj_config.py             城市配置（LJ_CITY）和可选的区县列表
│   ├── lj_sitemap.py            ① sitemap 全量 URL 收集（十几次请求拿到整个城市的房源链接）
│   ├── lj_capture_accounts.py   ② 账号登录状态抓取（循环开浏览器，等你扫码登录）
│   ├── lj_supervisor.py         ③ 顶层看门狗：崩溃自动重启调度器
│   ├── lj_scheduler.py            调度器：探测账号 + 三会话并行 + 段间休息
│   ├── lj_crawl_v5.py             采集主程序：账号池/轮换/调速/熔断/看门狗
│   ├── lj_account_pool.py         账号池：指纹分配/日配额/封禁冷却
│   ├── lj_captcha.py              极验 v4 自动过验证（Chrome CDP + ONNX 模型）
│   ├── lj_parse_detail.py         详情页 HTML 解析（46 个字段）
│   ├── lj_to_excel.py           ④ CSV → 格式化 Excel
│   └── lj_crawl_v3.py / v4.py / lj_collect_urls.py / lj_human_session.py   历史版本，仅供参考
├── models_repo/
│   ├── geetest_solver/          YOLO 检测 + OCR + 语序模型源码
│   └── models/                  ONNX / pkl 模型文件
├── .tmp/                      运行状态（Cookie、账号池、断点、日志），自动生成，不入库
└── 输出/                       采集结果 CSV / Excel，自动生成，不入库
```

## 环境准备

- Python 3.10+
- Google Chrome 和对应版本的 [chromedriver](https://googlechromelabs.github.io/chrome-for-testing/)
- 目前只在 Windows 上测试过（`lj_capture_accounts.py` 用 PowerShell 关闭浏览器进程）

```bash
pip install -r requirements.txt
```

城市和路径都用环境变量配置，不用改代码：

| 变量 | 说明 | 不设置时 |
|---|---|---|
| `LJ_CITY` | 要采集的城市代码，即 `https://<代码>.lianjia.com` 里那一段，如 `bj` `sh` `gz` `sz` `hz` `cd` | `bj` |
| `CHROME_PATH` | chrome.exe 的完整路径 | `%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe` |
| `CHROMEDRIVER_PATH` | chromedriver 的完整路径 | 从系统 PATH 里找 `chromedriver` |

## 快速开始

```bash
# 0. 选城市（Windows PowerShell 用 $env:LJ_CITY="sh"；Linux/macOS 用 export LJ_CITY=sh）
set LJ_CITY=sh

# 1. 收集全量房源 URL（约 1 分钟）→ 输出/ershoufang_urls_sitemap.csv
python scripts/lj_sitemap.py

# 2. 抓取账号登录状态：每个账号扫一次码，例如抓 1~5 号
python scripts/lj_capture_accounts.py 1 5

# 3. 打开过验证用的 Chrome（保持运行，最小化即可）
chrome.exe --user-data-dir=<任意空目录> --remote-debugging-port=9223 https://<城市代码>.lianjia.com/ershoufang/

# 4. 启动全自动采集（看门狗 → 调度器 → 三会话并行）
python scripts/lj_supervisor.py

# 5. 采完后导出 Excel
python scripts/lj_to_excel.py
```

单独跑一个采集会话也可以：

```bash
python scripts/lj_crawl_v5.py --in 输出/ershoufang_urls_sitemap.csv --out 输出/details.csv
```

## 核心设计

| 层 | 机制 |
|---|---|
| 指纹层 | curl_cffi 伪装 Chrome TLS/HTTP2 指纹，每个账号用不同版本 |
| 调速层 | 对数正态随机间隔（中位 8 秒，夜间 12 秒），偶尔长停顿 |
| 验证层 | ONNX 模型识别点选/语序题，语序题识别不了会刷新换题，模拟真人鼠标轨迹 |
| 账号层 | 多账号轮换，单号日配额 1300，被封自动冷却 24 小时 |
| 可靠性 | 断点续传 + 请求挂死看门狗 + 进程三级自愈 |

## 多台电脑并行

同一个账号在两地同时登录容易触发风控，所以多台电脑要**各用各的账号**：

1. 把 `输出/ershoufang_urls_sitemap.csv` 按行拆成几份（例如 part1 / part2）
2. 每台电脑各自抓自己的账号，用 `--in` 指定自己那一份
3. 采完后把各台的 `details_*.csv` 放到一起，运行 `lj_to_excel.py` 合并

## 注意

- `.tmp/lj_accounts/` 里是账号登录凭据（Cookie），**不要外传**，已写进 `.gitignore`
- 模型里的 `.pkl` 文件用 Python pickle 加载，只用本仓库自带的或你自己训练的，**不要换成来路不明的 pkl 文件**
- 换城市只要改 `LJ_CITY`；`lj_config.py` 里的 `DISTRICT_SLUG`（区县 → 拼音代码）是可选的，只有旧版 `lj_collect_urls.py` 必须填

## 许可证

[MIT](LICENSE)。代码可以自由使用、修改和分发，但需保留版权声明；作者不对使用后果承担任何责任。上面的免责声明同样适用。
