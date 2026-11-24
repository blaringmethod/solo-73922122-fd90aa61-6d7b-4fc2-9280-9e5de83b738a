# 巡天曝光 UTC 台账

按 UTC 日期领取曝光明细，用于对齐多台望远镜的观测。Flask 网页取数，SQLite 保存曝光 CSV 原值；正闰秒日按 86401 秒计算，区间一律左闭右开。

## 启动

```bash
docker compose up --build
```

打开 <http://localhost:8000>。数据保存在命名卷 `ledger-data`（容器内 `/data/ledger.sqlite3`），重启不丢。

不使用 Docker 时：

```bash
python3 -m pip install -r requirements.txt
LEDGER_DB=./var/ledger.sqlite3 gunicorn --bind 127.0.0.1:8000 --workers 1 --threads 8 wsgi:app
```

测试：`python3 -m pytest -q`（需要 `pip install pytest`）。

## 页面

- **取数**：选 UTC 日期，查看与该日相交的曝光，显示当天 UTC 起止时刻、实秒范围与每条曝光的裁剪后起止时刻和实秒数；可下载当天 CSV。
- **导入**：上传或粘贴 CSV，整批校验，要么全写入要么全回滚。提供示例 CSV。
- **闰秒表**：查看/替换生效中的正闰秒日期表，附版本历史。

页面：`/`（取数）、`/import`、`/leap`、`/day.csv?date=YYYY-MM-DD`、`/healthz`。

## CSV 格式

表头固定为：

```csv
telescope,exposure_id,start,duration_s
T1,EXP-0001,2026-11-24 00:00:00,600
T2,EXP-0002,2026-11-24 23:59:60,1
T3,EXP-0003,846201600,900
```

- `telescope`：望远镜号，1～64 字符。
- `exposure_id`：唯一曝光号，1～128 字符；批内重复或已入库即整批拒绝。
- `start`：二选一
  - UTC 文本 `YYYY-MM-DD HH:MM:SS`（也接受 `T` 分隔、结尾 `Z`）；`23:59:60` 只能出现在当前闰秒表中的闰秒日的 23:59:60。
  - 从 2000-01-01 00:00:00 UTC 起累计的整数实秒（≥0，日期不超过 9999-12-30）。
- `duration_s`：整数 1～3600。

导入按提交那一刻的生效闰秒表整批校验。CSV 原值整行保存在 `exposures.raw_json`，原始 `start` 词元另存 `start_raw`。

## 时间语义

- 实秒 = 从 2000-01-01 00:00:00 UTC 起经过的绝对秒数（相当于把闰秒计入时间轴）。
- 正闰秒日 00:00:00 起共 86401 实秒，最后一秒记为 `23:59:60`；该日区间为 `[当天 00:00:00, 次日 00:00:00)`。
- 单日取数只读 `start_real ∈ [当天起点 − 3600, 当天终点)` 的行，再用 `start_real < 当天终点 且 start_real + duration_s > 当天起点` 判定相交，并裁剪到当天。
- 每次取数只加载一个闰秒表版本（同一读事务中读取版本、边界、筛选与格式化），换表并发时不会混用两张表。

## 换表规则

- 提交内容整批校验（格式、日期合法、重复），任一行不合格整批拒绝。
- 换表与“已入库 UTC 文本是否仍合法”在同一事务中检查：若某条 `23:59:60` 的 UTC 文本在新表中不再是闰秒日，换表失败，页面列出曝光号，旧表继续生效。
- 以实秒导入的行是绝对时刻，换表不改变其 `start_real`；以 UTC 文本导入的行按新表重算实秒。
- 换表成功后新导入使用新表；历史版本可查。

## 并发

- 写操作（导入、换表）串行化：进程内锁 + SQLite `BEGIN IMMEDIATE`，全批原子提交。
- 读操作使用 WAL 快照，可在导入/换表进行中继续取数。
- 交付使用 gunicorn 单 worker 多线程；SQLite 文件放在单卷上，避免多 worker 争锁。

## 已知限制

见 `NOTES.md` 的“待完成事项”。
