# CloudCost

用 Python 3.11+ 编写的命令行费用监控工具，SQLite 存储，附带无需 Node 构建的 SPA。

代码仓库：<https://github.com/geekdada/cloudcost>；容器镜像：`ghcr.io/geekdada/cloudcost`。

监控当月累计费用：数据源明确提供未出账金额时使用 `unbilled_mtd`；否则使用 UTC **自然月累计** `calendar_mtd`，从月初统计到当前可用数据，不因已付款或月中出账而扣减。支持总和预算、平台独立预算、多账号、固定 USD/CNY 汇率、常见通知渠道和 SPA 查询。

## 平台接入与实际限制

| 平台 | 原生接入方式 | 费用口径与限制 |
| --- | --- | --- |
| AWS | Cost Explorer `GetCostAndUsage` | 自然月 `UnblendedCost` 累计估计费用；默认 USD。需启用 Cost Explorer 与 `ce:GetCostAndUsage` 权限。 |
| 阿里云国内站 `aliyun` | BSS `QueryAccountBill` | 当月 `BillingCycle` 的 `PretaxAmount`（优惠后、税前），默认 CNY。不是 `PaymentAmount`。 |
| 阿里云国际站 `alibabacloud` | 国际站 BSS `QueryAccountBill` | 同上，独立 endpoint 和凭据，默认 USD。 |
| Vercel | 官方 `GET /v1/billing/charges` | 按 `ChargePeriodStart/End` 汇总 JSONL 明细的 `BilledCost`，包含正负费用更正；自然月累计，默认 USD。配置 token 和 team 即可，无需 bridge。 |
| Cloudflare | 优先官方 `GET /accounts/{account_id}/billable-usage`（v1），必要时尝试受限 v2 成本查询 | 按日汇总 `BilledCost`，以费用发生日期归属自然月。需要 Billing Read 权限；固定套餐费用另行配置。 |

五个平台默认都使用 `mode = "native"`，也都可切换为 JSON/CSV 文件或 HTTPS feed。原生接口没有逐笔未出账状态，因此全部如实标记为 `calendar_mtd`。数据库、API、CSV 导出、SPA 与报警都保留实际口径；不同口径合计为 `mixed_mtd`。旧数据库和已有 `unbilled_mtd` feed 可以继续使用。

Cloudflare 优先使用 **v1 GET `/billable-usage`**，该接口提供已定价的每日费用。查询起点提前 31 天，包含可能位于上月的订阅计费周期起点，再按 `ChargePeriodStart/End` 筛出目标自然月；只相加每日 `BilledCost`，不相加周期累计字段 `CumulatedContractedCost`。这样不会遗漏非月初账期，也不会把上月费用计入当月。

若 v1 返回 HTTP 403/404/405，再尝试 v2 的只读 POST `/billable/usage`，`Metric=cost`。v2 为 Alpha / Restricted，文档中声明的 POST 在部分账号实际会返回 405；v2 GET 则只返回用量，不能用于计算金额。接口不可用、返回空集或缺少金额时报告失败，不当作零元。

Cloudflare 原生采集默认 `complete=false`，因为用量金额不包括固定套餐。核对所有固定套餐费用后，可配置 `fixed_monthly_cost = "5"`（原币）补充，或无固定费时显式填写 `"0"`，然后启用完整预算判断。这个值是**整个自然月的固定费用估计**，每月全额计入，不按天摊销；年付、月中升级、退款等需自行调整，或改用完整 feed。固定费用配置不会解决未开放或缺失用量金额的问题。

官方接口说明：[Cloudflare v1 日费用查询](https://developers.cloudflare.com/api/resources/billing/subresources/usage/methods/paygo/)、[Vercel FOCUS 费用查询](https://vercel.com/docs/rest-api/reference/endpoints/billing/list-billing-charges)、[Cloudflare Billable Usage](https://developers.cloudflare.com/api/resources/billing/subresources/usage/methods/get_account_usage_v2/)、[Cloudflare 官方 OpenAPI（含 POST 查询定义）](https://github.com/cloudflare/api-schemas/blob/main/openapi.yaml)、[Cloudflare 用量费用的覆盖范围](https://developers.cloudflare.com/billing/manage/billable-usage/)。

0.2.1 修复 Cloudflare HTTP 405：优先使用 v1 GET，覆盖非月初计费周期，并在两个接口均不可用时提示各自的 HTTP 状态。0.2.0 用户更新镜像并重新创建容器即可，配置无需修改。

从 0.1.0 升级时：SQLite 会自动迁移并保留历史记录和投递状态；配置不会被覆盖。要启用新的原生采集，把已有 Vercel / Cloudflare 的 `mode = "feed"` 改为 `"native"`，移除 `path/url`，按下文配置 token、team / account 和固定月费。已有 feed 继续有效。

## 快速体验

```bash
cd /workspace/cloudcost
python -m venv .venv
source .venv/bin/activate
pip install -e ".[aws]"

cloudcost init --demo -c demo.toml
cloudcost serve -c demo.toml
```

打开 <http://127.0.0.1:8765>。演示模式生成六个月的累计费用快照，使用单独的 `demo.sqlite3`，不会发送外部通知。`serve` 默认只查询，不自动采集。

在源码目录中也可以用 `python -m cloudcost` 替代 `cloudcost`。

## 使用真实数据

```bash
cloudcost init -c config.toml
# 编辑 config.toml，配置账号、阈值，禁用不使用的平台。
# 在本地环境或受保护的环境文件中设置凭据。
export AWS_PROFILE=billing-readonly
export VERCEL_TOKEN='你的 Vercel token'
export VERCEL_TEAM_ID='team_你的团队 ID'
export CLOUDFLARE_API_TOKEN='具有 Billing Read 权限的 token'
export CLOUDFLARE_ACCOUNT_ID='32 位账号 ID'
export ALIYUN_ACCESS_KEY_ID='你的国内站 AK'
export ALIYUN_ACCESS_KEY_SECRET='你的国内站 secret'
export ALIBABACLOUD_ACCESS_KEY_ID='你的国际站 AK'
export ALIBABACLOUD_ACCESS_KEY_SECRET='你的国际站 secret'

cloudcost collect -c config.toml
cloudcost status -c config.toml
cloudcost check -c config.toml --dry-run
cloudcost run -c config.toml --once --dry-run

# 验证金额与接入配置之后，启动定时采集和真实报警：
cloudcost run -c config.toml
# 另一终端启动只读查询界面：
cloudcost serve -c config.toml
```

也可以只运行 `cloudcost serve -c config.toml --monitor`，同时提供查询界面和后台监控。部署时选择一种监控方式，避免启动多个循环。

未使用的平台设置 `enabled = false`。所有启用的平台都需要数据，否则总预算判断暂停。一个平台多个账号时使用不同的 `id`，例如 `aws-prod`、`aws-dev`，相同的 `kind = "aws"`；账号都计入总和，独立预算按 `id` 判断。不要把管理账号汇总费用与其子账号费用同时接入，避免重复计算。

## 阈值与汇率

```toml
[monitor]
database = "cloudcost.sqlite3"
interval_seconds = 3600
stale_after_hours = 26

[fx]
USD = "1"
CNY = "7.2" # 1 USD = 7.2 CNY

[budget]
total = "1000"
currency = "USD"

[[providers]]
id = "aliyun"
kind = "aliyun"
mode = "native"
threshold = "1000" # 国内站默认 CNY
# threshold_currency = "USD" # 可独立指定阈值币种
```

金额使用 `Decimal` 计算，SQLite 使用文本保存精确十进制。总和先将原币金额换算成 USD，再比较总预算币种；不同采集时间的汇率不会混用。查询和报警使用当前配置汇率，数据库另保留采集时的汇率和 USD 折算金额。默认不会访问汇率服务。

未设置 `budget.total` 或某个平台的 `threshold` 时，该预算判断不启用。**严格大于**阈值才报警，等于阈值不报警。退款、更正造成负数或累计值下降是允许的。

## 可选 feed 数据源契约

JSON 是某个账号在某个月份的**一个累计快照**，文件可含单个对象，也可含不同月份的对象数组。同一月份只允许一个对象。参考 [examples/unbilled-feed.json](examples/unbilled-feed.json)：

```json
{
  "month": "2026-10",
  "basis": "unbilled_mtd",
  "amount": "118.90",
  "currency": "USD",
  "complete": true,
  "observed_at": "2026-10-08T12:00:00Z"
}
```

- `amount`：按声明口径从月初至 `observed_at` 的累计金额，不是当日增量。建议字符串，避免浮点精度损失。
- `basis`：`unbilled_mtd` 表示来源已排除已出账费用；`calendar_mtd` 表示按 UTC 自然月的费用发生日期累计，包含已出账部分。不能用付款时间或发票日期代替费用发生时间。
- `complete`：来源是否覆盖该账号所有目标费用；只有 Workers 等部分产品的数据应设为 `false`，不能宣称是 Cloudflare 总费用。
- `observed_at`：上游金额实际的截至时间，必须带时区、位于该月份且不在未来。不得每次抓取时把旧数据的时间改为当前时间。
- `currency`：必须与账号配置一致，只支持 USD/CNY。

```toml
[[providers]]
id = "vercel"
kind = "vercel"
mode = "feed"
threshold = "100"
path = "feeds/vercel-{month}.json"
# 上游每小时更新此文件（建议写入临时文件后原子替换）。
```

或从 HTTPS 获取（`path` 与 `url` 只能配置一个）：

```toml
[[providers]]
id = "cloudflare"
kind = "cloudflare"
mode = "feed"
currency = "USD"
threshold = "100"
url = "https://你的费用桥接服务/cloudflare/{month}"
token_env = "CLOUDFLARE_FEED_TOKEN" # 可选 Bearer 认证
format = "json"
```

`{month}` 自动替换为 UTC 当月，例如 `2026-10`。本地路径相对于配置文件所在目录，跟当前工作目录无关。HTTPS 证书验证始终启用，不跟随重定向。GET 数据采集遇到网络失败、429、5xx 最多尝试三次。

CSV 使用 `format = "csv"`，列为 `month,amount,currency,basis,complete,observed_at`。它代表**一次完整导出的费用明细**，目标月份的行金额会相加。不要把每天的累计快照混在这个 CSV 里，否则会重复累计。参考 [examples/unbilled-line-items.csv](examples/unbilled-line-items.csv)。普通发票 CSV、FOCUS `BilledCost` CSV 不会直接接受。

## 原生账号配置

Vercel 配置 `token_env = "VERCEL_TOKEN"`，以及 `team_id_env = "VERCEL_TEAM_ID"`、`team_id` 或 `slug`。团队费用接口需要有相应 Billing 查询权限的用户 token。使用 `BilledCost` 而非 `EffectiveCost`，避免把摊销金额与其他平台的普通月累计混用；前者是计费金额，不表示已支付。

Cloudflare 配置 `token_env = "CLOUDFLARE_API_TOKEN"` 与 `account_id_env = "CLOUDFLARE_ACCOUNT_ID"`，或直接配置 `account_id`。需要 Billing Read 权限，优先使用 PayGo v1 费用接口，受限 v2 仅作备用。默认配置故意不猜固定费：在该 provider 中填写经过核对的 `fixed_monthly_cost`，才能把用量费与固定费用估计作为完整月度预算比较。

```toml
[[providers]]
id = "cloudflare"
kind = "cloudflare"
mode = "native"
threshold = "100"
account_id_env = "CLOUDFLARE_ACCOUNT_ID"
token_env = "CLOUDFLARE_API_TOKEN"
fixed_monthly_cost = "5" # 核对实际套餐；没有固定费则填 "0"
```

AWS 使用 boto3 默认凭据链，支持环境变量、共享 profile、SSO 和实例/任务角色。可在单个 provider 中指定 `profile`。Cost Explorer 默认 region 为 `us-east-1`，成本指标可选 `UnblendedCost`、`NetUnblendedCost`、`AmortizedCost`、`NetAmortizedCost`。不同指标会改变预付、抵扣和摊销口径，应统一配置。AWS Cost Explorer 查询可能产生 API 费用，默认每小时采集。

阿里云的 AccessKey 应配置账单只读权限，例如 `AliyunBSSReadOnlyAccess`（按账号权限体系核对）。国内/国际站凭据分别使用 `ALIYUN_*` 和 `ALIBABACLOUD_*` 环境变量。可配置 `security_token_env` 接入临时 STS 凭据，也可用 `access_key_env`、`secret_key_env` 自定义变量名。国际站默认 endpoint 为 `https://business.ap-southeast-1.aliyuncs.com`；区域或合约币种不同时可显式调整 `endpoint` 与 `currency`。

阿里云自动分页并汇总 `PretaxAmount`；这包含未支付金额，且不扣除已付款来伪造未出账余额。所有原生 collector 只读取当前月份，历史月份通过已保存的 MTD 快照查询。

云平台有自身费用更新延迟。本工具的“新鲜度”对 feed 使用上游 `observed_at`；对原生 API 使用请求时间，**这不保证云端用量已经实时更新**。没有返回费用记录时显示缺失，不能推断费用是零。

## 通知渠道

配置任意多个 `[[channels]]`，每个 `name` 唯一。Webhook、机器人 token、签名 secret 和 SMTP 密码均从环境变量读取，不出现在查询 API 中。

| type | 必要配置 | 可选配置 |
| --- | --- | --- |
| `console` | `name` | 输出到 stderr，不破坏 stdout JSON |
| `webhook` | `url_env` | `token_env`，POST `budget.exceeded` JSON |
| `slack` | `url_env` | Incoming Webhook |
| `discord` | `url_env` | Discord Webhook |
| `telegram` | `token_env`, `chat_id_env` | 默认 `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` |
| `dingtalk` | `url_env` | `secret_env`，支持加签；机器人关键词包含 `CloudCost` |
| `feishu` | `url_env` | `secret_env`，支持加签 |
| `wecom` | `url_env` | 企业微信机器人 |
| `email` | `host`, `from`, `to` | `port`, `tls`, `username_env`, `password_env` |
| `ntfy` | `url_env` | 指向 HTTPS topic；可选 `token_env` |

```toml
[[channels]]
name = "slack-ops"
type = "slack"
url_env = "SLACK_WEBHOOK_URL"

[[channels]]
name = "telegram-ops"
type = "telegram"
token_env = "TELEGRAM_BOT_TOKEN"
chat_id_env = "TELEGRAM_CHAT_ID"

[[channels]]
name = "mail-ops"
type = "email"
host = "smtp.example.com"
port = 587
tls = "starttls" # 或 ssl（默认端口 465）
from = "cloudcost@example.com"
to = ["ops@example.com"]
username_env = "SMTP_USERNAME"
password_env = "SMTP_PASSWORD"
```

去重键是 `(月份, 平台/总和, 阈值, 阈值币种)`，按通知渠道分别记录。成功的渠道不再重复发送；失败的渠道在下一轮仍超限且数据有效时重试；修改阈值或新月份会产生新事件。同一阈值下回落后再次超限仍不重复发。进程中断后的投递租约为两分钟。

HTTP 通知一次检查只请求一次；部分机器人会 HTTP 200 返回业务错误，本工具也会识别失败。外部通知无法保证网络超时/进程崩溃情况下严格恰好一次，对方已收到而本地尚未记成功时，重试可能重复。`--dry-run` 不投递，也不占用正式去重状态；`collect --dry-run` 仍会采集并保存费用。

## CLI 与查询 API

```bash
cloudcost collect --check # 一次采集后检查预算
cloudcost check --dry-run
cloudcost status --month 2026-09
cloudcost history --month 2026-09 --provider aws --limit 200
cloudcost alerts
cloudcost run --once     # 适合 cron
cloudcost serve --monitor
```

所有命令支持 `-c/--config`，可放在子命令前或后。stdout 输出 JSON，日志和控制台报警输出 stderr。退出码：`0` 成功；`1` 采集/投递失败或预算判断因数据不完整暂停；`2` 配置/命令错误。发现超预算但成功投递不是进程错误。配置变更需重启。

SPA 支持月份切换、平台筛选、USD/CNY 展示切换、累计趋势、独立预算、CSV 导出、报警与采集记录，60 秒刷新查询。页面是只读查询，不触发费用采集或通知。趋势按每日已知的最新值组合，不代表每个平台在同一秒更新；默认展示最近 5,000 条快照。

| GET 路径 | 参数 |
| --- | --- |
| `/api/health` | 无 |
| `/api/months` | 无 |
| `/api/summary` | `month=YYYY-MM` |
| `/api/history` | `month`, `provider`, `limit`（最多 5,000） |
| `/api/alerts` | `month`, `limit`（最多 1,000） |
| `/api/collections` | `limit`（最多 1,000，跨月份的最近采集记录） |

默认监听 `127.0.0.1:8765`。对外监听必须配置 `CLOUDCOST_API_TOKEN`：

```bash
export CLOUDCOST_API_TOKEN='一个足够长的随机访问令牌'
cloudcost serve --host 0.0.0.0
```

API 使用 `Authorization: Bearer ...`，SPA 会提示输入令牌并仅存于当前浏览器会话。生产环境对外访问应由反向代理提供 HTTPS。通知和云凭据不会提供给浏览器。

## Docker 与 GitHub 自动发布

镜像默认以 UID/GID `10001:10001` 运行，已包含 AWS SDK。配置、SQLite 和费用 feed 存放在持久化 `/data` 中，镜像默认启动查询页面与后台监控：

```text
cloudcost serve -c /data/config.toml --host 0.0.0.0 --monitor
```

首次使用必须先生成并编辑配置。仓库/镜像私有时，先使用具有 `read:packages` 权限的 GitHub PAT 登录 GHCR（不要把 token 写进仓库）：

```bash
printf '%s' "$GHCR_TOKEN" | docker login ghcr.io -u geekdada --password-stdin
git clone https://github.com/geekdada/cloudcost.git
cd cloudcost
cp .env.example .env
# 编辑 .env：设置长随机 CLOUDCOST_API_TOKEN 和实际需要的云账号/通知凭据。
docker compose pull
docker compose run --rm cloudcost init -c /data/config.toml
docker compose run --rm --entrypoint cat cloudcost /data/config.toml > config.toml
# 编辑 config.toml 中的平台凭据、阈值及 Cloudflare 固定月费。
docker compose run --rm -T --entrypoint python cloudcost -c \
  "import sys; from pathlib import Path; Path('/data/config.toml').write_text(sys.stdin.read(), encoding='utf-8')" < config.toml
docker compose up -d
```

打开 <http://127.0.0.1:8765>，输入 `.env` 中的 API token。Compose 默认仅将端口发布到主机 localhost，外部访问可接反向代理。使用命名 volume 保留 SQLite；不要执行 `docker compose down -v`，除非确定要删除监控数据。`.env`、用户配置和数据库不会提交 Git，也不会放入镜像。

演示模式可使用独立的数据卷：

```bash
docker run --rm -v cloudcost-demo:/data ghcr.io/geekdada/cloudcost:latest \
  init --demo -c /data/config.toml
docker run --rm -p 127.0.0.1:8765:8765 -v cloudcost-demo:/data \
  -e CLOUDCOST_API_TOKEN ghcr.io/geekdada/cloudcost:latest \
  serve -c /data/config.toml --host 0.0.0.0
```

上述演示命令要求当前 shell 已设置 `CLOUDCOST_API_TOKEN`。镜像也可直接执行 CLI，例如 `docker run --rm -v cloudcost-data:/data --env-file .env ghcr.io/geekdada/cloudcost:latest status -c /data/config.toml`。若使用主机目录代替命名卷，目录须允许 UID 10001 读写。可以只读挂载 AWS profile 目录到 `/data/.aws`；runtime 的 home 目录是 `/data`。

[GitHub Actions 工作流](.github/workflows/docker.yml) 在 `main` 推送、`v*` 标签推送或手动执行时自动：

1. 在 Python 3.11 / 3.12 上运行完整测试并检查 SPA JavaScript。
2. 构建 AMD64 镜像，检查 CLI、非 root 用户、演示数据库、API 认证和 SPA。
3. 使用仓库内置的 `GITHUB_TOKEN` 登录 GHCR，发布 AMD64 / ARM64 多架构镜像。

Pull request 执行验证但不推送镜像。默认分支生成 `latest`，每次发布生成 `sha-<短提交>`；如推送 `v0.2.0`，另生成 `0.2.0` 和 `0.2`。版本标签需要显式推送，不会自动创建。镜像带 GitHub 源仓库与提交元数据，发布摘要中显示 digest。无需额外配置 Docker Hub token 或云账号 secrets；云凭据仅在实际运行时注入。首次 GHCR package 默认私有，公开仓库也不保证 package 自动公开，可在 GitHub Packages 设置里另行调整。

本地构建：

```bash
docker build -t cloudcost:local .
```

使用代理且需要自定义 CA 时可指定 `--secret id=proxy_ca,src=/path/to/ca-bundle.pem`。证书仅临时挂载在安装依赖步骤，不会写入镜像层。

## 部署与验证

常驻运行 `cloudcost run`，或 cron：

```cron
0 * * * * /opt/cloudcost/.venv/bin/cloudcost run -c /etc/cloudcost/config.toml --once >> /var/log/cloudcost.log 2>&1
```

systemd 示例见 `deploy/`。示例假设安装目录 `/opt/cloudcost`、服务账号 `cloudcost` 和 `/etc/cloudcost/config.toml`。配置中将 SQLite 路径设置为该账号可写的目录，如 `/var/lib/cloudcost/cloudcost.sqlite3`；费用 feed 路径、环境文件权限和 AWS profile 均须可被服务账号访问。监控和 Web 服务共享同一 SQLite 文件。

```bash
python -m unittest discover -s tests -v
node --check cloudcost/static/app.js
```

启动演示服务之后可以验证浏览器交互：

```bash
pip install -e ".[test]"
playwright install chromium
python tests/browser_smoke.py --url http://127.0.0.1:8765
```

测试使用临时 SQLite、HTTP mock 和 SMTP mock，涵盖费用口径、精确换算、快照不重复累加、完整性、阈值、投递去重/失败重试、并发抢占、原生请求构造、分页、查询验证和 API 认证。没有访问真实云账号或发送真实通知。浏览器端验证脚本见 `tests/browser_smoke.py`，需要安装 Playwright 与 Chromium。

费用接口文档参考：[AWS Cost Explorer](https://docs.aws.amazon.com/aws-cost-management/latest/APIReference/API_GetCostAndUsage.html)、[阿里云 QueryAccountBill](https://api.aliyun.com/api/BssOpenApi/2017-12-14/QueryAccountBill)、[Vercel FOCUS billing charges](https://vercel.com/docs/rest-api/billing/list-focus-billing-charges)、[Cloudflare Billable Usage](https://developers.cloudflare.com/billing/manage/billable-usage/)。
