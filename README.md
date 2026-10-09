# ReelReady

[English](README.en.md) | 中文

院线电影一般要过一段时间才会上线网络，上线后 PT 站才会出现 WEB-DL 等资源。ReelReady 会**定期收集国内外院线的高分电影**，持续检测它们的上线状态，并在 PT 站一出现符合要求的资源时**自动推送到 qBittorrent 下载**，同时发邮件通知你。

## 功能

- **自动收集**：豆瓣「正在热映」和 TMDB 多个地区的院线片，按评分和评分人数筛选（豆瓣和 TMDB 分开计算，满足任一即可），自动跳过重映老片
- **待确认 / 监测中 / 已完成 / 黑名单**：收集到的片先进「待确认」，你点一下才开始监测；移入黑名单的片不会再被收集；也可以粘贴豆瓣 / TMDB / IMDb 链接手动添加
- **上线检测**，状态只往前走：`院线中 → 已定档 → 已上线 → 已下载`
  - 已定档：TMDB 有数字版 / 实体版发行日期
  - 已上线：TMDB（JustWatch 数据）显示可在线观看、租或买，或豆瓣出现「在线观看」平台（腾讯视频、爱奇艺等）
- **PT 搜索 + 自动下载**：支持馒头（官方 API）和 NexusPHP 站点。每次扫描从所有站点的结果里挑出最符合规则的一个种子，推送到 qBittorrent
- **CookieCloud 同步 cookie**：在浏览器里登录 PT 站就行，cookie 自动同步过来；ReelReady 还会从同步的 cookie 里自动识别你登录过的 NexusPHP 站
- **邮件通知**：成功提交下载、下载失败、站点登录失效时单独发邮件；定档、上线、新的待确认影片每天汇总一封。提交下载邮件标注事件发生时间，通知逐封保存发送状态并防止并发重复发送。内置 QQ、163、126、Gmail、iCloud、Outlook 等常见邮箱的 SMTP 预设，支持多个收件人
- **动态管理**：支持手动删除记录；保留天数留空表示永久保留，填写后每天自动清理到期动态
- **设置自动保存**：地区与清晰度可排序；后台任务按顺序执行，重复请求不会反复排队
- **开机补跑**：电脑不是 24 小时开着也没关系，启动后会补上错过的检测

## 选种规则（默认值，都能在设置里改）

| 规则 | 默认 |
|---|---|
| 允许的清晰度（按优先级） | 2160p > 1080p，720p 及以下不下 |
| 排除 | 枪版（CAM / TS / TC 等）、Remux、原盘 |
| 体积上限 | 30 GB |
| 排序 | 做种数 → 清晰度 → 中字优先 → HDR / 杜比视界优先 |

当前稳定版本为 **1.0.0**。可使用 `ghcr.io/muzi-xiaoren/reelready:1.0.0` 固定版本，或 `:latest` 跟随最新发布；镜像支持 `linux/amd64` 和 `linux/arm64`。

## 快速开始（Windows + Docker Desktop）

1. 安装并启动 [Docker Desktop](https://www.docker.com/products/docker-desktop/)，在它的设置里勾上「Start Docker Desktop when you sign in」，这样开机后 ReelReady 会自动运行
2. 新建一个文件夹（例如 `D:\ReelReady`），把仓库里的 [`docker-compose.yml`](docker-compose.yml) 放进去
3. 在该文件夹里打开终端运行：

   ```bash
   docker compose up -d
   ```

4. 打开 <http://localhost:8765>

数据库、cookie 和配置都保存在该文件夹下的 `data/` 目录里，删掉或升级容器都不会丢。

更新到新版本：

```bash
docker compose pull
```

```bash
docker compose up -d
```

## 首次配置

在「设置」页依次完成：

1. **TMDB**：到 [themoviedb.org](https://www.themoviedb.org/settings/api) 免费申请 API Key 并填入。国内直连 TMDB 不稳定，可以在「网络」里填代理地址（例如 Clash：`http://host.docker.internal:7890`）
2. **qBittorrent**：在 qBittorrent 的「工具 → 选项 → Web UI」里启用 Web 用户界面，然后填入地址（默认 `http://host.docker.internal:8080`，即宿主机）、用户名和密码。保存路径和分类都可以留空
3. **邮件通知**：选择邮箱服务商，填邮箱账号和授权码（QQ / 163 在邮箱网页版的设置里开启 SMTP 后获取授权码；Gmail / iCloud 需要「应用专用密码」）。收件人可以填多个，留空则发给自己。保存后点「发送测试邮件」确认

在「站点」页添加 PT 站：

- **馒头**：在馒头网页的「控制台 → 实验室 → 存取令牌」生成 API Key，填入即可
- **Rousi Pro**：使用新版 PeerGo API v1。选择「Rousi Pro (API Key)」，填写账户设置中创建的个人 API Key（需要 profile:read、torrent:read、torrent:download 权限）。浏览器 Cookie 不替代 API Key；下载通过详情接口返回的临时签名链接完成。
- **Monikadesign**：可用 CookieCloud 同步登录 Cookie，或手动选择「Monikadesign (Cookie)」添加；已提供 Unit3D 网页登录检查、搜索和种子下载适配。
- **NexusPHP 站（包括 QingWa）**：推荐用 CookieCloud 自动同步（见下一节），也可以手动粘贴 cookie

## 用 CookieCloud 同步 cookie

[CookieCloud](https://github.com/easychen/CookieCloud) 是一个开源的浏览器扩展，可以把浏览器里的 cookie 加密后同步到服务器。ReelReady 内置了兼容 CookieCloud 协议的服务端，不需要再单独部署。

1. 在 Edge 里打开 Chrome 应用商店安装 CookieCloud（Edge 支持安装来自 Chrome 应用商店的扩展）
2. 打开 ReelReady 的「站点」页，复制「服务器地址」「用户 KEY」「端对端加密密码」填到扩展里，工作模式选「上传到服务器」
3. 「同步域名关键词」建议填你的 PT 站域名（每行一个），留空会同步全部 cookie
4. 在扩展里点「手动同步」，刷新「站点」页，识别出的 NexusPHP 站会出现在「添加站点」里，点一下就能添加

之后你只要在浏览器里正常登录 PT 站，扩展就会定时把新 cookie 推给 ReelReady。如果 cookie 失效，ReelReady 会发邮件提醒你；在浏览器里重新登录一次，下次同步后就会自动恢复。

站点识别结合本地 [PT-Depiler](https://github.com/pt-plugins/PT-depiler) 公开站点库和 NexusPHP Cookie 特征，不需要向第三方发送 Cookie。只把含非空、未过期登录 Cookie 的站点列为候选，浏览标记、广告及 Cloudflare Cookie 不作为登录依据。服务端是否仍认可登录，需要添加后测试；其他架构的 PT 站单独列为浏览记录。添加会立即入列，连接测试在独立后台队列进行（最多 2 个并行、16 个等待及测试中任务），页面自动更新测试结果；相同站点及已知别名由数据库唯一约束防止重复添加。漏识别的站点可选择「NexusPHP」，在地址框中选择已同步域名或手动填写。站点库来源版本记录在 `reelready/sites/catalog.json`，MIT 声明保存在同目录的 `PT_DEPILER_LICENSE.txt`；可用 `python tools/import_pt_depiler.py <PT-Depiler源码目录>` 更新本地站点库。

> cookie 以加密形式保存在本机的 `data/` 目录，不会上传到任何第三方。

## 工作流程

```
收集（默认每 24 小时）          上线检测（默认每 6 小时）           PT 搜索（默认每 2 小时）
豆瓣正在热映 ┐                 TMDB 数字版日期 / 流媒体平台         馒头 API / NexusPHP 搜索
TMDB 院线     ┼→ 待确认 →(批准)→ 豆瓣在线观看平台          ──┐      按 IMDb 精确匹配，按规则选种
按评分筛选    ┘                  状态推进时记录事件          │  →  推送到 qBittorrent → 邮件通知
                                                             └── 每日汇总邮件（默认 9 点）
```

所有间隔都能在设置里修改；每个任务也可以在「影片」页顶部手动立即运行。

## 本地开发

需要 Python 3.12 及以上：

```bash
pip install -r requirements.txt
```

```bash
python -m reelready
```

默认把数据写到当前目录的 `data/` 下，可以用环境变量 `REELREADY_DATA_DIR`、`REELREADY_PORT` 修改。

## 说明

- 请遵守各 PT 站的规则。ReelReady 只搜索「监测中」的影片，每次请求之间有间隔（默认 5 秒），尽量避免给站点造成压力
- 本项目只做信息聚合和下载器调度，不提供任何影视资源

## License

[Apache-2.0](LICENSE)
