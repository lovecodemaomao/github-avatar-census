# GitHub 头像世代统计

比较近期公开开源贡献者在不同 GitHub 注册年份组的**当前头像**。这不是对所有程序员的随机抽样，也不能解释同一人换头像的历史。

## 运行

需要 Python 3.12、已登录的 `gh` CLI，以及本机 Ollama。

```powershell
python -m pip install -r requirements.txt
ollama pull qwen3-vl:4b
python census.py discover --per-stratum 20
python census.py commits
python census.py users-batch
python census.py select-users
python census.py avatars
python census.py classify-pipeline
python census.py audit-export
python census.py report
```

所有步骤可以重复运行；SQLite 保留已完成的仓库、提交窗口、用户、头像和分类。采集提交时也可运行 `users-batch` 预取已发现的账号，所有提交采完后再运行一次 `users-batch` 和 `select-users`，保证最终抽样覆盖完整候选池。`users` 是逐个 REST 查询的备用方式，可用 `--max-requests N` 限制单次请求数；`classify-pipeline` 先快速分类，再对无法判断项做完整推理，两步都可续跑。人工复核时，参照 `data/audit/` 中的头像拼图，在 `data/audit/audit_sample.csv` 的 `human_label` 列填入 `human/anime/character/meme/animal/logo/illustration/default_abstract/uncertain`，然后执行：

```powershell
python census.py audit-import data/audit/audit_sample.csv
python census.py report
```

`data/` 只在本机保存原始头像、账号清单、复核材料与可追溯证据；`results/` 只包含汇总、图片与报告。不要将 `data/` 中的头像当作可自由再发布的素材。

## 已生成的结果与公开范围

公开仓库可包含 [`results/report.md`](results/report.md)、[`results/summary.json`](results/summary.json) 和两张汇总图。完整抽样记录、账号清单、原始头像及人工复核拼图保存在被 Git 忽略的 `data/`，不会随仓库上传。因此公开结果可以检查统计口径和汇总数，但不能仅凭公开仓库逐人复算本次样本；在本机则可从 SQLite 追溯。

报告中的主比例来自本地视觉模型分类，人工抽查每组只有 30 张，最新组的人工抽查并未复现模型所示的逐组上升。请同时阅读报告的人工复核和抽样局限，不要将结果表述为所有程序员的趋势。

## 抽样口径

从 Python、JavaScript、TypeScript、Go、Rust、Java 六种主语言与三个星数区间形成 18 个层，每层固定随机种子抽 20 个仓库。过去一年分四个两周窗口，采集默认分支的提交，按 GitHub 关联的作者账号去重。全部候选账号资料抓完后，按注册年份分成五组，在每组用固定随机种子抽至多 300 人；组织、机器人与无法取得资料的账号排除。搜索排序、仓库选择、窗口截取和公开提交可见性都会造成偏差，因此结果仅描述该样本。记录仓库、搜索条件、窗口状态及每条提交证据以便复核。

视觉模型在本机运行，输出九类标签。分母采用可判断的头像数量，同时单列无法判断和下载/分类失败。Wilson 区间仅给出样本比例的不确定性，不解决抽样偏差或仓库内相关性。

分类方案是在 `data/audit/` 的固定 150 张单人盲审头像上确定的：奇数编号 75 张用于选择方案，偶数编号 75 张留作评估。保留的 `qwen3-vl_4b_trial.csv`、`qwen3-vl_4b_nothink_trial.csv` 和 `pipeline_trial.csv` 记录了模型与两步规则的逐张结果；运行 `python build_pipeline_trial.py` 可复算两组指标。第一步若给出 `uncertain`，第二步才启用完整推理；其余标签直接保留。

API 字段与限制以 [GitHub 用户查询](https://docs.github.com/en/graphql/reference/users)、[REST 提交接口](https://docs.github.com/en/rest/commits/commits) 和 [GitHub 限流说明](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api) 为准。本地分类使用 [Ollama Qwen3-VL 4B](https://ollama.com/library/qwen3-vl)。
