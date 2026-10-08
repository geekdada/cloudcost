# Cloudflare Compose 部署

只启用 Cloudflare，配置内嵌在 `compose.yml`；SQLite 自动创建在持久化卷中。需要 Docker Compose 2.23.1 或更新版本。

```bash
cp .env.example .env
openssl rand -hex 32
# 编辑 .env，把生成的随机值填入 CLOUDCOST_API_TOKEN。
# 填入 Cloudflare token、account ID、预算和固定套餐月费总和。
docker compose up -d
docker compose logs -f cloudcost
```

访问 <http://127.0.0.1:8765>，输入 `.env` 中的 `CLOUDCOST_API_TOKEN`。默认报警输出到容器日志；启用 Slack 时，取消 `compose.yml` 中 Slack channel 的注释，并在 `.env` 中填写 `SLACK_WEBHOOK_URL`。修改配置后重新执行 `docker compose up -d`。

`CLOUDFLARE_FIXED_MONTHLY_COST` 是整个自然月固定套餐费的原币估计，不按天摊销。确实没有固定费时填 `0`。账号需要有 Billing Read 权限；优先调用 PayGo v1 日费用接口，v1 不可用时才尝试 Alpha / Restricted v2。没有金额数据时不能视为零费用。

更新镜像并重新创建容器：

```bash
docker compose pull
docker compose up -d --force-recreate
```

预览采集与报警（不会发送通知）：

```bash
docker compose run --rm cloudcost collect -c /etc/cloudcost.toml --check --dry-run
```

镜像私有时，先使用有 `read:packages` 权限的 GitHub PAT 登录 GHCR：

```bash
printf '%s' "$GHCR_TOKEN" | docker login ghcr.io -u geekdada --password-stdin
```

本例将端口绑定在主机 localhost；对外访问可接反向代理。命名卷保存历史数据，`docker compose down -v` 会删除该卷。
