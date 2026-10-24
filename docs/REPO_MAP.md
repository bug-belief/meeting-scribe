# REPO_MAP · 符号地图（自动生成，勿手改）

> 由 `tools/repo_map.py` 生成于 2026-10-22，按**被引用次数**降序。改了代码就重跑：
> `python tools/repo_map.py --write`。检查是否过期：`python tools/repo_map.py --check`。
>
> 用途：新接手时先看这里，`grep` 查「叫什么」、这张表查「改哪个最危险」。

## `kb_search.py`

- `search(pack, query, top_k, min_score, …)` L524 ·引用 37 — BM25 + 标题/标签加成
- `tokenize(text)` L268 ·引用 11 — 中文二元组 + 英文词
- `split_chunks(body)` L335 ·引用 10 — 按 markdown 标题切块；无标题的长文按段落兜底切
- `ALL_AUDIENCES` L179  ·引用 9
- `expand_legal(query)` L125 ·引用 7 — 口语查询 → 补充的法律术语（只返回扩展出来的部分）
- `phrase_bonus(core, text, cap)` L137 ·引用 6 — 查询与文本的最长公共子串 / cap
- `STOP_BIGRAMS` L49  ·引用 5
- `LEGAL_SYNONYMS` L73  ·引用 5
- `build_kb_pack(kb_root, include_secret, redact, redact_min_severity)` L378 ·引用 4 — 个人 KB → 索引包
- `PACK_VERSION` L37  ·引用 3
- `DEFAULT_TOP_K` L38  ·引用 3
- `MAX_CHUNK_CHARS` L39  ·引用 3
- `CJK_RE` L45  ·引用 3
- `ASCII_RE` L46  ·引用 3
- `SCOPE_TERMS` L163  ·引用 3
- `clean_snippet(text, limit)` L310 ·引用 3 — 把片段洗成「能给模型看的一句话」
- `load_pack(path)` L673 ·引用 3
- `default_pack()` L682 ·引用 3
- `MIN_CHUNK_CHARS` L42  ·引用 2
- `LEGAL_COOCCUR` L103  ·引用 2
- `RECALL_K` L119  ·引用 2
- `EXT_WEIGHT` L122  ·引用 2
- `ALL_SCOPES` L178  ·引用 2
- `PROVENANCE_MARKERS` L303  ·引用 2
- `build_flat_pack(root)` L459 ·引用 2 — 扁平语料（法律条文等）：目录下每个 .md 视为一条，文件名即 id
（另有 8 个符号引用 ≤1，`--budget` 加大可见）

## `transcribe.py`

- **`Segment`** L69 ·类·0 个公开方法 ·引用 42
- `ASSISTANT_SPEAKER` L43  ·引用 18
- `ASSISTANT_LABEL` L44  ·引用 11
（另有 12 个符号引用 ≤1，`--budget` 加大可见）

## `utils.py`

- **`PipelineError`** L25 ·类·0 个公开方法 ·引用 44 — 可预期的流水线错误（用于区分「音频本身有问题」与「代码 bug」）
- `get_logger(name)` L83 ·引用 32
（另有 19 个符号引用 ≤1，`--budget` 加大可见）

## `config.py`

- **`Settings`** L92 ·类·0 个公开方法 ·引用 63
- `SETTINGS` L285  ·引用 44
（另有 11 个符号引用 ≤1，`--budget` 加大可见）

## `tools/repo_map.py`

- `est_tokens(s)` L59 ·引用 10 — 粗估 token：中文字符按 1、其余按 4 字符 1 token
- `collect()` L120 ·引用 5 — 返回 (符号表, 引用计数)
（另有 8 个符号引用 ≤1，`--budget` 加大可见）

## `tools/bench_llm.py`

- `report(rows)` L205 ·引用 8
- `call(key, base, model, messages, …)` L56 ·引用 2 — 发一次请求，返回统一结构（含 reasoning tokens 与耗时）
（另有 4 个符号引用 ≤1，`--budget` 加大可见）

## `tools/check-dead-settings.py`

- `CONFIG` L48  ·引用 2
- `SKIP_PREFIXES` L51  ·引用 2
（另有 6 个符号引用 ≤1，`--budget` 加大可见）

## `websearch.py`

- `search(query, count)` L374 ·引用 37 — 真正发请求
- `extract_query(text)` L166 ·引用 15 — 从一句口语里剥出可检索的关键词
（另有 23 个符号引用 ≤1，`--budget` 加大可见）

## `redact.py`

- **`Rule`** L219 ·类·0 个公开方法 ·引用 12
- `SEVERITY_ORDER` L46  ·引用 7
（另有 32 个符号引用 ≤1，`--budget` 加大可见）

## `costs.py`

- `llm_cost(model, prompt_tokens, completion_tokens, cache_hit_tokens, …)` L203 ·引用 14 — 一次 LLM 调用的价钱
- `read_records(ref, since)` L342 ·引用 13 — 读回用量记录
（另有 15 个符号引用 ≤1，`--budget` 加大可见）

## `live_engine.py`

- `SAMPLE_RATE` L33  ·引用 43
- **`LiveSession`** L104 ·类·11 个公开方法 ·引用 13
（另有 17 个符号引用 ≤1，`--budget` 加大可见）

## `kb_bridge.py`

- `hits(lines, ask, query, top_k, scenario)` L229 ·引用 55 — 检索命中
- `resolve_scenario(name)` L161 ·引用 10 — 把档名解析成可直接用的过滤条件
（另有 15 个符号引用 ≤1，`--budget` 加大可见）

## `api/deps.py`

- `tenant(request)` L49 ·引用 30 — 按当前请求的 owner 派生目录配置（归档 / 工作目录 / 声纹库全按它隔离）
- `meeting_id(folder)` L171 ·引用 21 — 从归档目录的 meta.json 取 meeting_id；取不到返回空串
（另有 11 个符号引用 ≤1，`--budget` 加大可见）

## `voice_corpus.py`

- `ASSISTANT_SPEAKER` L52  ·引用 18
- `MAX_SEG_SEC` L54  ·引用 8
（另有 9 个符号引用 ≤1，`--budget` 加大可见）

## `summarize.py`

- **`LLMClient`** L123 ·类·1 个公开方法 ·引用 12
- `split_chunks(body, limit)` L72 ·引用 10 — 按说话人轮次分块，尽量不把同一段话切断
（另有 8 个符号引用 ≤1，`--budget` 加大可见）

## `tts_server.py`

- `SYSTEM_VOICES` L55  ·引用 8
- `DEFAULT_PROFILE` L49  ·引用 3
（另有 3 个符号引用 ≤1，`--budget` 加大可见）

## `server.py`

- `_VOICE_MAP` L80  ·引用 10
- `WEB_DIR` L65  ·引用 8
- `FAVORITE_NAME` L72  ·引用 8
（另有 2 个符号引用 ≤1，`--budget` 加大可见）

## `pipeline.py`

- **`StateStore`** L41 ·类·3 个公开方法 ·引用 8 — 记录每个文件哈希的处理状态，防止重启/重传导致重复处理
- `process_audio(audio, state, settings, dual, …)` L223 ·引用 6 — 处理一个音频
（另有 2 个符号引用 ≤1，`--budget` 加大可见）

## `speakers.py`

- **`SpeakerStore`** L284 ·类·13 个公开方法 ·引用 17 — 声纹库
- `CHANNEL_THRESHOLDS` L41  ·引用 14
（另有 14 个符号引用 ≤1，`--budget` 加大可见）

## `tools/verify_search_key.py`

- `parse(provider, raw)` L47 ·引用 6 — 按我的 websearch.py 里的解析逻辑提取，验证结构是否匹配
- `ENDPOINTS` L32  ·引用 3
（另有 2 个符号引用 ≤1，`--budget` 加大可见）

## `voice_register.py`

- `probe_duration(path, ffprobe)` L46 ·引用 6
- `sh(cmd)` L42 ·引用 4
（另有 3 个符号引用 ≤1，`--budget` 加大可见）

## `web/make_icons.py`

- `make(size, maskable)` L17 ·引用 7
- `BLUE` L12  ·引用 3
（另有 1 个符号引用 ≤1，`--budget` 加大可见）

## `tools/speaker_cli.py`

- `cmd_list(args, settings)` L143 ·引用 1
- `cmd_show(args, settings)` L159 ·引用 1
（另有 5 个符号引用 ≤1，`--budget` 加大可见）

## `android/make_launcher_icons.py`

- `HERE` L10  ·引用 3
- `DENSITIES` L16  ·引用 3

## `tools/check-schemas.py`

- `MAX_PROBLEMS` L46  ·引用 3
- `check_metas(tenants)` L49 ·引用 1
（另有 2 个符号引用 ≤1，`--budget` 加大可见）

## `watcher.py`

- **`InboxHandler`** L33 ·类·3 个公开方法 ·引用 1 — 只负责把候选路径丢进队列，重活交给 worker
- **`Watcher`** L57 ·类·3 个公开方法 ·引用 1

## `schema_guard.py`
> 共列出 71 个符号，估算 1994 tokens（预算 2000）。
