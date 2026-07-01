# 公开部署运维说明（market.meltemi.vip）

本目录提供常驻部署与磁盘运维的示例配置。`./run.sh` 的前台 `wait` 适合本地
一次性运行，不适合公开 VPS 长期常驻；生产请用 systemd 常驻 + cron 清理缓存。

## 1. 常驻服务（systemd）

见 `screener.service`。要点：

- `Restart=on-failure`：崩溃自动拉起，避免服务挂掉后长期不可用。
- `server.py` 默认绑定 `127.0.0.1`，前面用 Nginx/Caddy 做反代 + TLS。
- 通过环境变量启用安全/计费保护（见下）。

```bash
sudo cp deploy/screener.service /etc/systemd/system/screener.service
sudo systemctl daemon-reload
sudo systemctl enable --now screener.service
journalctl -u screener.service -f
```

## 2. 安全 / 计费保护环境变量

`server.py` 读取以下环境变量（均可选，未设时保持本地开发默认行为）：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `SCREENER_TOKEN` | 空（不鉴权） | 一旦设置，所有写/计费端点（refresh、deep、cbond refresh/deep、layer4）必须带正确 token：请求头 `X-Auth-Token`、`Authorization: Bearer <token>` 或 query `?token=`，否则 401。 |
| `SCREENER_COOLDOWN` | 60 | 计费端点最短再次触发间隔（秒），按端点+参数维度。 |
| `SCREENER_MAX_CONCURRENCY` | 2 | 同时运行的抓取/AI subprocess 上限，超限返回 429。 |
| `SCREENER_DAILY_QUOTA` | 0（不限） | 每日计费调用配额，超额返回 429。 |
| `SCREENER_DEBUG` | 关 | 开启后错误响应才回吐 stderr/stdout 片段；生产保持关闭，避免泄漏内部路径。 |
| `SCREENER_DEEP_FRESH` | 21600（6h） | 深研 JSON 新鲜度阈值（秒）：新鲜才复用旧数据（`--ai-only`），否则重抓量化数据。 |
| `SCREENER_CORS_ORIGINS` | 空（同源） | CORS Origin 白名单（逗号分隔完整 Origin）；不在白名单不下发 `Access-Control-Allow-Origin`。 |

响应默认附带 `X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY` 与基本 CSP。

## 3. 缓存清理与磁盘监控

见 `cache-cleanup.cron`。长期运行 `cache/` 会持续增长，需定期清理：

```cron
30 3 * * * find /opt/economy/cache -type f -mtime +14 -delete 2>/dev/null
```

`run.sh` 每次启动也会清理 `cache/`（`CACHE_MAX_AGE_DAYS`，默认 14 天），但
systemd 常驻不重跑 run.sh，故必须额外配 cron。建议同时监控磁盘占用（示例中
`df` + `logger`），>85% 告警。

## 4. 验收校验（CI/部署门禁）

```bash
python3 scripts/acceptance_check.py --strict-artifacts --http
```

`--strict-artifacts` 会在任一市场 stale/空结果时失败，适合部署前把关；
默认（不带该 flag）保持宽松，允许缺产物用于本地探索。
