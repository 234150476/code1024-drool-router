# 独立质量巡检服务

本目录基于 `noonwake-ai/ai-drool-router` 提交 `a86d96a0e494a99437841a0c9fddf73c7b0c33aa` 二开，保留原 LGPL-3.0-or-later 许可与署名。
不包含 Sub2API 补丁，不要求更换 Sub2API 程序；检测、SQLite、页面和发布由本服务独立维护。

本二开由 `daQzi/code1024-drool-router` 独立发布，当前版本为 `v0.2.0`。安装该版本时使用本仓库源码：

```sh
git clone --branch v0.2.0 https://github.com/daQzi/code1024-drool-router.git
cd code1024-drool-router
```

已有部署升级时保留自己的 `config.json`、`credentials/` 和数据卷，先备份再切换程序镜像。
生产 `/quality/` 子路径部署仍须以 `DROOL_BASE=/quality/` 构建前端。

## 页面地址

- 独立打开：`http://127.0.0.1:4191/`
- 嵌入浅色：`http://127.0.0.1:4191/?embed=1&theme=light`
- 嵌入深色：`http://127.0.0.1:4191/?embed=1&theme=dark`
- 正式部署支持独立 HTTPS 域名或同域子路径；当前为 `https://api.code1024.cc/quality/?embed=1`。

127.0.0.1 指浏览器所在机器。服务器部署后使用可访问的域名；HTTPS 的 Sub2API 页面应嵌入 HTTPS 服务。
在 config.json 配置允许嵌入的 **Sub2API origin** 和本服务自己的外部 origin：

```json
{
  "web": {
    "frame_ancestors": ["https://panel.example.com"],
    "public_origin": "https://inspect.example.com"
  }
}
```

origin 包含协议、域名、可选端口，没有路径或末尾斜杠。不接受通配符；默认仅同源可嵌入。
Sub2API 侧用你现有的自定义页面配置允许此来源，本服务不会修改它。
跨域不能自动读取宿主主题，使用 theme 参数指定；页面内也可切换。

当前控制台公开页无需登录，管理与本人 Key 检测使用宿主 Bearer 会话，不依赖第三方 Cookie。宿主 iframe 应允许脚本与服务本身的正常 origin，
否则浏览器会拦截页面 API 请求。服务内部的模型作品仍使用无 allow-same-origin 的 sandbox。
公开作品按当前发布状态逐次校验，撤回后不再提供。新版内联作品使用空 sandbox 和禁止外部资源的 CSP，不执行模型生成的脚本。

### code1024 管理员会话

设置 `web.session_auth_url` 为可信 code1024 内部地址后，服务会逐次向其 `/api/v1/auth/me`
验证 Bearer 会话，仅接受 active 管理员。code1024 的独立页面会自动传入当前 token 和主题；
本页面读取后立即从自身 URL 清除 token、user_id，以请求头继续访问 API。反向代理必须关闭
该路径的 query 访问日志，并保留 `Referrer-Policy: no-referrer`。不要通过聊天或普通链接分享带 token 的 URL。

手动同步、检测仅将已验证会话传给当前 worker 的环境变量，不落盘为网关凭据。
本模式无需创建长期管理员 Key；会话失效后重新进入 code1024 独立页面。
单独打开无 token 的服务地址可以查看公开结果，不能访问管理配置与本人 Key。
后台定时任务需要单独配置机器凭据，不能使用浏览器会话。升级不自动启用定时与隔离。

## v0.2 管理流程

1. 在“统一题库”配置题目、提示词、标准答案与独立回答次数。内置糖果、鹈鹕骑自行车、逻辑选择、数值、JSON 和指纹观察；可新增、停用。相同内容有固定版本，历史任务保留原题目快照。
2. 在“检测计划”选择真实分组或账号、排除项、各平台模型与推理等级、题目。保存后预览账号与请求上限，确认后执行。预览后题库/清单/参数变化会要求重新预览。
3. 在“调度与自动化”输入网关管理员 API Key，验证元数据与账号导出权限后保存到 `/data/private/automation-key`（0600）。也可继续使用部署环境变量/文件。若网关要求账号导出二次验证，保持保护并使用下述本地凭据文件。
4. 机器凭据就绪后，在计划中配置常规/安静时段间隔，以及隔离策略的连续失败轮数、冷却分钟数和每组最低可用账号。默认关闭自动隔离，旧版 priority/callable 开关不能启用。
5. 标准答案题明确答错才累积连续失败。400、鉴权、网络、限额、无法判定和参考题不触发质量停调度。绘图“通过”只表示 SVG 结构完整。
6. 隔离持久化、写入 `schedulable=false` 并回读。冷却后保持不调度直接复测，所有持有隔离的计划通过后才恢复；账号被人工修改、计划变化或回执不明确时转“需要管理员核对”。“接管此账号”仅释放服务的自动恢复权，随后在 code1024 手动处理。

隔离会影响此账号的全部分组，因此最低可用数保护检查其全部真实分组。网关布尔接口没有 CAS，仍有跨服务并发窗口；恢复前校验更新时间和账号状态，不保证跨服务原子事务。服务单实例运行。

公开页按真实分组查看时间轴、模型、题目版本、来源、耗时及可公开作品/源码。按账号运行的结果也关联到账号真实分组，不公开账号身份。只有计划勾选“公开回答/作品”才提供对应正文。

登录用户通过本人 Key 选择同一题库检测，原始 Key 只在服务端内存使用。默认私有；明确勾选“公开摘要与作品到 Key 所属分组”后才公开正文。旧版已公开自测保持仅摘要。Key 分组在执行前复查；归档时固定当时所属分组。用户自测与管理员账号直连分别标注，用户结果不能停用任何具体后台账号。

升级新增题库文档、隔离/证据表与 self_jobs 的快照/分组/内容发布列，不删除原数据。升级前停任务并备份数据卷；回滚旧镜像保留新列，但新题型计划和隔离策略需要先停用。未完成的手动任务不会自动重发；重启中断的冷却复测会等待新一段冷却后进入新周期，写入中断转人工核对。

## 本机运行

Python 3.10+（本次验证 3.12），Vite 7 要求 Node 20.19+ / 22.12+。保留本项目 npm 锁文件。

```sh
cp config.example.json config.json
python3.12 -m venv .venv
.venv/bin/python -m pip install -r detector/requirements.txt
npm --prefix web ci
npm --prefix web run build
./scripts/start-local.sh
```

也可用 uv 创建环境与安装依赖。默认只监听本机，不带口令时仅接受回环 Host。
启动生成真实空状态，不填入演示数据；示例的定时、回写、所有平台默认关闭。

## 连接网关

在 config.json 填 base_url、启用模型及小范围分组。其余平台保持 enabled=false。

```json
{
  "base_url": "https://your-sub2api.example.com",
  "platforms": {
    "openai": {
      "enabled": true,
      "model": "填写账号实际支持的模型 ID",
      "effort": "medium",
      "group_ids": [1]
    }
  },
  "schedule": {"enabled": false},
  "routing": {"write_priority": false, "write_callable": false}
}
```

空 group_ids/group_names 表示该平台所有分组。按实际账号适配器验证模型，不按名字猜能力。
`upstream.codex_version` 可配置 Codex HTTP 客户端版本（当前默认 `0.169.0`），应与宿主网关生效版本保持一致。
旧客户端可能拿到较小的模型清单并被拒绝使用账号实际拥有的模型；这种 400 不代表账号套餐为 Free。
用户自助检测通过 `/api/v1/keys` 读取本人 Key，浏览器仅获取脱敏元数据。
管理员凭据存 credentials/sub2api-admin，设置 SUB2API_ADMIN_KEY_FILE 为该文件的绝对路径。
用 `openssl rand -hex 24 > credentials/web-token` 生成服务口令，DROOL_WEB_TOKEN_FILE 指向它。
也支持 SUB2API_ADMIN_KEY / DROOL_WEB_TOKEN 环境变量，服务口令至少 24 字符。
凭据文件 chmod 600，并避免提交 Git（credentials/ 已忽略）。重启后先点“同步账号”，它只读元数据，不调用模型。

“运行本轮检测”会先显示成本确认，再发起真实请求；已完成的同一时间槽不会重跑。
通常每账号为两次逻辑题和一次绘图，首次答错提前结束，重试可能增加请求数。
直连上游消耗其额度，可能不计入 Sub2API 正常网关计费与业务日志。

独立控制台每 30 秒检查每个计划的时间槽并去重；控制台隔离使用跨任务持久化证据，与原命令行 routing 两个开关独立。

## 账号导出有二次认证时

原项目通过 /api/v1/admin/accounts/data 导出凭据；开启二次认证后，机器管理员 Key 可能返回 403。
保持原有认证策略，由管理员通过原授权流程取得所需配置，另存私有本地文件：

```json
{
  "7": {
    "name": "与 Sub2API 账号名称完全一致",
    "platform": "openai",
    "credentials": {"api_key": "填写真实凭据", "base_url": "https://api.openai.com"}
  }
}
```

以数字账号 ID 为 key，名称/平台必须一致。配置代理的账号还需完整 proxy 对象
（沿用上游 proxy_url 的 protocol/host/port/username/password 字段）。
设置 SUB2API_ACCOUNT_CREDENTIALS_FILE 指向此文件，worker 会跳过在线凭据导出。
缺项、身份不匹配或代理缺失会拒绝检测，不会改用别的账号。
管理员 Key 仍用于元数据、设置/配额和可选回写，受限 Key 权限不足时会报错。

本地 OAuth access_token 需自行更新。本工具未接入宿主 token 刷新、出站插件和全部客户端身份策略，
探针直连与真实业务经网关的结果应分别验证。Anthropic OAuth 不受支持，Gemini CLI / Antigravity OAuth
不能当普通 Gemini API Key 使用。建议先用一个 API Key 账号验收，再逐类扩展。

## Docker 与域名

建议独立放在 /opt/stacks/ai-drool-router/：

```sh
cp config.example.json config.json
mkdir -p credentials
openssl rand -hex 24 > credentials/web-token
# 在 credentials/sub2api-admin 填入自己的管理员凭据。
docker compose build
docker compose up -d
```

容器内监听 0.0.0.0:4191，宿主只映射 127.0.0.1:4191，由现有 HTTPS 反代提供独立域名。
容器内的 127.0.0.1 不是宿主，网关应使用实际域名或按网络环境使用 host.docker.internal。
镜像以 drool 用户运行；用专用用户/组或 ACL 确保 config 和 credentials 可读，不能把密钥改成所有人可读。
参考 deploy/nginx.conf.example，不要在反代上添加阻止嵌入的 X-Frame-Options 或重复 CSP。

SQLite 与结果保存在 drool-data，配置/凭据独立挂载。更新可重建镜像，回滚保留数据卷。
这是单主机、单数据目录运行器，多节点需要额外分布式协调，不能跨主机直接共享 SQLite。
本版使用服务内调度器，不用容器重启模拟定时器，也不给一次性 worker 套 HTTP 健康检查。
开启回写时，倍率改变后下一轮检测重算；需要更及时重算可显式使用原 pricing_tick 命令。

### 当前生产部署（2026-11-16）

- 页面入口：`https://api.code1024.cc/custom/quality-inspection`，管理员菜单“质量巡检”。
- 服务地址：`https://api.code1024.cc/quality/?embed=1`；前端以 `DROOL_BASE=/quality/` 构建。
- 服务器目录：`/opt/stacks/ai-drool-router/`，生产配置 `compose.yaml`、`config.json`。
- 容器名 `ai-drool-router`，端口 `127.0.0.1:4191`；只读根文件系统、非 root 用户，数据卷 `ai-drool-router_drool-data`。
- 共享网络 `qimu_qimu-net`，网关与会话验证均用 `http://code1024:8080`，不经过公网。
- 默认范围 OpenAI / Plus 分组 ID 3，模型 `gpt-6-astra`，推理等级 `medium`；已同步 4 个账号元数据。
- `schedule.enabled=false`、`routing.write_priority=false`、`routing.write_callable=false`。未运行真实模型检测。
- 通过现有管理 API 只更新 `custom_menu_items`，未修改、重建或重启 code1024。

Nginx 实际配置为 `/opt/stacks/nginx/config/sites-available/api.code1024.cc.conf`，
`/etc/nginx/sites-enabled/api.code1024.cc.conf` 为软链。`/quality` 跳转至 `/quality/`，
`location ^~ /quality/` 使用 `proxy_pass http://127.0.0.1:4191/;` 剥离前缀。
该 location 关闭 access_log，并把 error_log 等级设为 crit，避免嵌入初始 URL 的 query 被日常日志记录。

部署命令（在服务器目录执行）：

```sh
docker compose -f compose.yaml config --quiet
docker compose -f compose.yaml build
docker compose -f compose.yaml up -d
docker compose -f compose.yaml ps
```

备份放在服务器 `deployment/`，权限 0600：`settings.menu-before.json` 保存菜单修改前设置，
`nginx.before.20261116224736.conf` 保存反代修改前配置。回滚时仅移除菜单 ID `quality-inspection`，
或将备份中的 `custom_menu_items` 单独 PUT 回管理 API；不要把整个旧设置覆盖回去。
Nginx 回滚先核对当前文件的其他改动，再撤销本服务两个 location，`nginx -t` 通过后 reload。
最后 `docker compose -f compose.yaml down` 停止独立服务，保留数据卷；不要使用 `down -v`。
`deployment/` 的临时验证会话在验收后删除，既有 code1024 登录文件不变。

## 检测参考与验证

页面提供逻辑/数学、JSON 指令、长上下文、代码测试建议，链接 GSM8K、IFEval、RULER、HumanEval。
这些是补充参考，未运行完整基准；轻量样例由本二开整理，不计入巡检分数，也不自动执行生成代码。
绘图通过只表示完整 HTML/SVG 已取出，没有自动视觉评分或模型身份鉴定。
请求故障、限额与答错单列；单次错误不证明降智，综合分含价格与速度，不是纯智力分。

```sh
.venv/bin/python -m unittest discover -s detector/tests
npm --prefix web run build
cd web && node --test src/*.test.js
```

本地测试使用隔离 fixture，不接收费模型。UI 演示用 scripts/dev_preview.py，有明确演示标记，和真实空状态分开。

### 本次本机验证记录

- Python 3.12：310 项单元/HTTP 测试通过；前端 43 项测试及 Vite 子路径生产构建通过。
- 原隔离 E2E 通过：模拟账号的连续答对、首次答错结束、上游故障、禁用回写零写入与开启回写后的回读。
- Chromium 实测浅/深色、390px 无横向溢出、未配置状态禁止运行；跨域 iframe 中输入口令后能加载数据与签名作品，未在白名单的父页面被拦截。
- Docker Compose 配置校验、生产镜像构建及容器健康检查通过；已连接真实网关做元数据同步，未调用收费模型。
- 线上 Chromium 实测管理员菜单与 iframe 登录通过，初始 token/user_id 已从子页面 URL 清除；4 个账号、3 个页签、4 类检测参考显示正常，深色跟随宿主，390px 下各页签无横向溢出，资源请求无 4xx/5xx、无页面脚本错误。
- 生产 worker 回报 `metadata_refreshed / accounts=4 / model_requests=0`，SQLite 实查 `runs=0`；匿名数据接口返回 401。现有 code1024、启幕与数据库容器保持健康。

## 公开结果与管理员控制台（2026-11-17）

自 `v0.1.1` 起，公开页固定按分组汇总，普通用户与匿名访客均不展示账号入口、账号别名、
账号列表、账号数量或账号与结果的对应关系。公开 overview、groups 及分组详情接口使用同一字段白名单，
旧的公开账号详情接口返回 404。历史 `show_accounts` 和账号别名配置保留兼容，不会扩大公开页的数据范围。
管理员仍可按分组或指定账号执行检测，并在管理员任务详情中查看真实账号及对应结果。
个人 Key 自助检测与用户主动公开的自测摘要保持独立，不暴露供应商账号信息。

分组页按相同模型、推理等级与题库展示通过率、样本数、请求故障、限额、耗时和最近检测时间。
请求故障与无样本不作为“降智”结论；底层仍为分组内账号直连探针，不能替代网关端到端质量检测。

`/quality/` 现在默认打开公开结果，不要求服务口令或登录。管理员从 code1024 的
`/custom/quality-inspection` 进入，宿主 JWT 经 `/api/v1/auth/me` 验证 active/admin 后，
才能同步清单、保存业务参数、按分组或账号运行检测、停止后续请求以及设置公开内容。
独立服务口令仅保留部署诊断用途，不能代替管理员身份修改新控制台。

普通用户菜单“模型质量”使用 `/custom/quality-results`，其自定义菜单配置应为：

```json
{
  "id": "quality-results",
  "label": "模型质量",
  "url": "https://api.code1024.cc/quality/?embed=1",
  "visibility": "user",
  "hide_open_button": true
}
```

外链嵌入菜单不要设置 `page_slug`；该字段会强制切换到内置 Markdown 页面，优先于 `url`。
`/custom/:id` 的路由由 `id` 关联菜单。若页面出现“目录”和“页面不存在”，先检查此字段。
修复现有菜单时只移除对应菜单的 `page_slug`，通过管理 API 单独更新 `custom_menu_items`，
保存前备份、保存后读回确认；不需要重启服务或发布 code1024。已打开的宿主页面需要刷新以重新加载菜单配置。

该问题已在线上按上述方式修复，读回确认只有 `quality-results` 菜单移除了 `page_slug`，其余设置不变，
本机及服务器部署辅助脚本也已纠正。浏览器以普通用户角色模拟会话加载线上真实菜单、资源和巡检 API，
修复前复现了错误的 `/api/v1/pages/quality-results` 请求，修复后验证分组、账号、检测参考和 390px 布局通过。
此次未使用其他用户的真实登录凭据。另以无会话浏览器验证公开接口 200、管理接口 401；有效管理员接口仍返回 200。

业务配置、检测计划、公开名称、任务及结果存于 `/data/private/console.sqlite3`。
`config.json` 继续负责内部地址、认证地址、监听、凭据路径、嵌入来源、身份盐等基础连接；
其中业务参数只用于首次初始化。之后请在管理页面修改。保存不会调用模型。
新任务固定计划、参数版本和去重后的账号快照，每任务使用独立 worker 数据目录，避免局部检测删除其他历史。
原 `/data/private/state.sqlite3` 原样保留；当前生产迁移前没有检测记录。

公开汇总 JSON 只包含明确发布的分组和统计数据。账号别名、真实账号名、内部 ID、凭据、代理和原始异常不公开。
答案与作品分别控制；每次访问详情和作品重新校验发布状态，撤回立即返回 404，响应为 no-store。
作品使用 sandbox 与禁网 CSP。按分组表示组内账号直连表现，并非网关入口链路检测。

手动任务只在内存与 worker 临时进程环境中使用短期管理员 JWT，不写入 SQLite 或配置快照。
定时执行需要部署机器凭据 `SUB2API_ADMIN_KEY_FILE` / `SUB2API_ADMIN_KEY`，未配置时界面禁止启用。
当前控制台只观察结果，调度回写未开放，开启回写字段会明确拒绝保存。
公开统计按模型、推理等级、题库和检测项目分开，费用未知显示未知。

本地验证：315 项 Python 测试、43 项 Node 测试、`DROOL_BASE=/quality/` Vite 构建。
新增集成测试使用本机 HTTPS 模拟上游与临时 CA，真实启动隔离 worker，验证指定账号、新模型、
历史保留、任务幂等、版本冲突、取消/重启、公开撤回及 HTTP 权限，无生产模型请求。
浏览器检查覆盖公开结果、详情、管理员保存、范围预览、运行确认、深浅主题和 390px 布局。

部署前备份卷和 compose；保留旧镜像。恢复旧镜像及 compose 即可回退程序，新数据库不改变旧库结构。
若需恢复全部业务配置，可停服务后恢复部署前卷备份；这会丢弃备份之后的新任务及参数。
