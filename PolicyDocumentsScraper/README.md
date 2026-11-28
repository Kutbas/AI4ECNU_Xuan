# PolicyDocumentsScraper

教育部政府门户网站（www.moe.gov.cn）"教育部文件"频道政策文件增量爬虫。

- 抓取 [教育部文件频道](http://www.moe.gov.cn/was5/web/search?channelid=239993) 的全部政策文件（当前约 13000+ 条，884 页）；
- 复刻详情页"下载"按钮的表单提交，获取每篇文档的 zip（内含正文 PDF 与全部附件），**解压到独立子目录**归档；
- 基于 SQLite 的增量状态管理：定期运行时只处理新发布的文档；
- 内置常驻定时模式（`loop`），亦可配合 cron / systemd 部署在 Linux 服务器；
- 磁盘占用保护：默认超过 **50 GiB** 自动停止采集（生产环境用 `--max-disk-gb 0` 解除限制）。

## 工作原理

1. **列表页**：`GET /was5/web/search?channelid=239993&page=N` 翻页枚举全部条目（每页 15 条，`page` 从 1 开始）；
2. **详情页**：`GET` 条目链接，提取标题、发文字号、发布时间（`#opertime`）等元数据；
3. **下载**：详情页的"下载"按钮实际是提交隐藏表单
   `POST http://www.moe.gov.cn/htmlToPDF/WebPd4MlPDFConvert.jsp`（字段 `url`、`opertime`），
   接口直接返回 zip 文件流，无需浏览器；
4. **解压**：zip 内文件名为 GBK 编码（无 UTF-8 标志位），解压时已做重解码修复与 zip-slip 路径防护；
5. **兜底归档**：极少数老页面的转换接口会永久性返回 550（实测约 0.3%），
   此时自动改用兜底方式——保存页面 HTML 快照（`page.html`）并直接下载正文附件，
   同样归档到独立子目录，保证 100% 覆盖；
6. **状态落库**：每篇文档的抓取状态记入 SQLite，供增量更新与断点续爬。

## 快速开始

本项目的 Python 环境与依赖由 [uv](https://docs.astral.sh/uv/) 管理：

```bash
uv sync                      # 创建虚拟环境并安装依赖
uv run scraper run --max-pages 1 --max-docs 3   # 试运行：抓 1 页列表、下载 3 篇
uv run scraper status        # 查看抓取状态
```

首次全量抓取（后台运行，建议在服务器上执行）：

```bash
nohup uv run scraper run --full > /dev/null 2>&1 &
```

> 全量约 13200+ 篇文档，默认 1s 请求间隔下预计 **15~24 小时**；之后每轮增量只需几分钟。
> 全量数据量预计 10~40 GiB，本地开发默认 50 GiB 阈值可能在中后段触发停止，属预期行为。

## 命令说明

### `run` —— 执行一轮抓取

```text
uv run scraper run [选项]

--full              全量模式：扫描全部列表页（关闭已知页早停），只补缺失文档
--max-pages N       最多扫描 N 个列表页
--max-docs N        本轮最多处理 N 篇文档（测试用）
--delay SECONDS     相邻请求基础间隔（默认 1.0，另加 0~0.5s 随机抖动）
--known-stop-pages N 连续 N 个整页均为已知文档时提前结束（默认 3；--full 时为 0）
--retry-failed      重试全部失败文档（默认每篇最多自动重试 3 次）
--no-download       仅抓列表建索引，不下载文件
--keep-zip          解压后保留原始 zip（默认删除以节省磁盘）
--max-disk-gb N     数据占用上限（GiB），超过即停止采集；0 = 不限制；默认 50
--data-dir PATH     数据目录（默认 ./data）
--verbose, -v       输出调试日志
```

### `loop` —— 常驻定时模式

```bash
uv run scraper loop --interval 21600    # 每 6 小时自动抓取一轮增量
```

支持 `run` 的全部选项；`Ctrl+C` / `SIGTERM` 优雅退出（完成当前文档后停止）。

### `status` —— 查看状态

```bash
uv run scraper status
```

展示各状态文档数量、数据占用与最近 5 轮运行记录。

## 数据目录结构

```text
data/
├── state.db                 # SQLite 状态库（文档索引 + 运行历史）
├── files/                   # 归档根目录：每篇文档一个独立子目录
│   ├── 1451040_教育部办公厅关于开展2026年"基础教育精品课"遴选工作的通知/
│   │   ├── 教育部办公厅关于开展2026年"基础教育精品课"遴选工作的通知 - ....pdf
│   │   ├── 精品课制作要求.docx
│   │   └── ...
│   └── ...
├── zips/                    # 仅 --keep-zip 时保留的原始 zip
└── logs/scraper.log         # 滚动日志（5MB × 3 份）
```

## 增量更新机制

- 文档以 URL 为主键登记入库，状态机：`pending → done / failed / missing / external`；
- 每轮从第 1 页开始扫描，**连续 3 个整页全部为已知文档**即提前结束——政策按时间倒序发布，
  正常情况下只需扫描前几页即可覆盖增量；
- `failed` 文档在后续轮次自动重试（每篇累计 3 次上限）；
  `--retry-failed` 无视上限，且**直接处理库内失败/待下载文档**，无需翻页扫描（修复大批量失败时速度快）；
- `missing`（详情页 404）与 `external`（站外链接）为终态，不再处理；
- 磁盘阈值按状态库累计字节数判断，超限当轮立即停止；下轮 loop 自动重试（若仍超限则继续跳过）。

## Linux 服务器部署

### 方式一：内置 loop 模式（最简单）

```bash
cd /opt/PolicyDocumentsScraper
nohup uv run scraper loop --interval 21600 --max-disk-gb 0 \
    >> data/logs/loop.out 2>&1 &
```

### 方式二：cron 定时任务

```bash
crontab -e
# 每 6 小时抓取一轮增量（生产环境不限磁盘）
0 */6 * * * cd /opt/PolicyDocumentsScraper && /usr/local/bin/uv run scraper run --max-disk-gb 0 >> data/logs/cron.log 2>&1
```

### 方式三：systemd（推荐生产环境）

`/etc/systemd/system/moe-scraper.service`：

```ini
[Unit]
Description=MOE policy documents scraper
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/opt/PolicyDocumentsScraper
ExecStart=/usr/local/bin/uv run scraper run --full --max-disk-gb 0
TimeoutStartSec=infinity
```

`/etc/systemd/system/moe-scraper.timer`：

```ini
[Unit]
Description=Run MOE scraper every 6 hours

[Timer]
OnCalendar=*-*-* 00/6:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now moe-scraper.timer
```

> 提示：`uv` 路径可用 `which uv` 确认；首次部署先手动跑一次 `uv sync` 建好环境。

## 数据迁移（本地全量 → 服务器）

如果已在本地完成全量抓取，可以把 `state.db`（状态库）与 `files/`（归档文件）直接搬到服务器，
服务器**无需重新爬取**，之后只运行增量更新即可：

```bash
# 1) 本地打包（Git Bash / Linux 均可，只打包状态库与归档，排除日志）
mkdir -p dist
tar -czf dist/moe_policy_data_$(date +%Y%m%d).tar.gz -C data state.db files
sha256sum dist/moe_policy_data_*.tar.gz > dist/moe_policy_data_$(date +%Y%m%d).tar.gz.sha256

# 2) 上传到服务器
scp dist/moe_policy_data_*.tar.gz* user@server:/tmp/

# 3) 服务器上校验并解压到项目 data 目录
cd /opt/PolicyDocumentsScraper
sha256sum -c /tmp/moe_policy_data_*.sha256          # 校验完整性
mkdir -p data && tar -xzf /tmp/moe_policy_data_*.tar.gz -C data

# 4) 核对：文档总数与本地一致即迁移成功
uv run scraper status

# 5) 配置定时增量（见上节三种方案，生产环境记得 --max-disk-gb 0）
```

要点：
- `state.db` 记录了每篇文档的 done 状态，增量运行时这些文档会被直接跳过；
- 解压完成后、落库前意外中断的文档由 `recovered` 机制自动补记；
- 若担心传输/解压过程有损坏，可运行完整性校验：

```bash
# 校验所有已归档目录；缺失/为空的会重置为待下载，再跑一次 run --full 即可补齐
uv run scraper repair
```

## 常见问题

- **文件名乱码？** 不会。zip 内 GBK 文件名已统一重解码后再落盘。
- **中断后如何续跑？** 直接重新运行同一命令即可，已完成的文档自动跳过；
  解压完成但未落库的目录也会被自动识别补记（recovered）。
- **Windows 路径过长？** 极少数超长标题会截断到 60 字符；如仍遇到问题，
  可在系统开启长路径支持（组策略 → 启用 Win32 长路径）。
- **频率限制？** 默认 1s + 随机抖动的请求间隔对该站是安全的；
  若出现大量 5xx/超时，可加大 `--delay`。

## 免责声明

本工具仅用于抓取教育部政府门户网站公开发布的政策文件，请合理控制抓取频率，
遵守站点相关使用条款。
