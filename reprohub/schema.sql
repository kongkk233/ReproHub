-- ReproHub SQLite schema（幂等）
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS sources (
  id INTEGER PRIMARY KEY,
  key TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  type TEXT NOT NULL,
  tier INTEGER NOT NULL DEFAULT 3,
  kind TEXT NOT NULL DEFAULT 'news',
  enabled INTEGER NOT NULL DEFAULT 1,
  url TEXT,
  interval_minutes INTEGER NOT NULL DEFAULT 360,
  last_fetch_at TEXT,
  next_fetch_at TEXT,
  last_status TEXT,
  last_error TEXT,
  last_new INTEGER NOT NULL DEFAULT 0,
  last_seen INTEGER NOT NULL DEFAULT 0,
  total_items INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY,
  source_id INTEGER NOT NULL REFERENCES sources(id),
  external_id TEXT,
  url TEXT NOT NULL,
  url_hash TEXT NOT NULL UNIQUE,
  doi TEXT,
  pmid TEXT,
  title TEXT NOT NULL,
  title_fp TEXT,
  title_zh TEXT,
  summary TEXT,
  summary_zh TEXT,
  authors TEXT NOT NULL DEFAULT '[]',
  journal TEXT,
  kind TEXT NOT NULL DEFAULT 'news',
  lang TEXT NOT NULL DEFAULT 'en',
  published_at TEXT,
  fetched_at TEXT NOT NULL,
  topics TEXT NOT NULL DEFAULT '[]',
  primary_topic TEXT,
  topic_score REAL NOT NULL DEFAULT 0,
  in_scope INTEGER NOT NULL DEFAULT 0,
  preference_score REAL NOT NULL DEFAULT 0,
  preference_labels TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'new',
  reject_reason TEXT,
  llm_score REAL,
  final_score REAL,
  score_dims TEXT,
  recommend_reason TEXT,
  keywords TEXT NOT NULL DEFAULT '[]',
  journal_key TEXT,
  journal_tier TEXT,
  journal_if REAL,
  selected INTEGER NOT NULL DEFAULT 0,
  selected_at TEXT,
  story_id INTEGER,
  llm_provider TEXT,
  llm_model TEXT,
  llm_is_mock INTEGER NOT NULL DEFAULT 0,
  llm_effort TEXT,
  scored_at TEXT,
  pushed INTEGER NOT NULL DEFAULT 0,
  extra TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_items_doi ON items(doi);
CREATE INDEX IF NOT EXISTS idx_items_pmid ON items(pmid);
CREATE INDEX IF NOT EXISTS idx_items_title_fp ON items(title_fp);
CREATE INDEX IF NOT EXISTS idx_items_published ON items(published_at);
CREATE INDEX IF NOT EXISTS idx_items_fetched ON items(fetched_at);
CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);
CREATE INDEX IF NOT EXISTS idx_items_selected ON items(selected, final_score);
CREATE INDEX IF NOT EXISTS idx_items_story ON items(story_id);
CREATE INDEX IF NOT EXISTS idx_items_topic ON items(primary_topic);
CREATE INDEX IF NOT EXISTS idx_items_source ON items(source_id);

CREATE TABLE IF NOT EXISTS item_mentions (
  id INTEGER PRIMARY KEY,
  item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  source_id INTEGER NOT NULL REFERENCES sources(id),
  url TEXT,
  seen_at TEXT NOT NULL,
  UNIQUE(item_id, source_id)
);

CREATE TABLE IF NOT EXISTS interpretations (
  id INTEGER PRIMARY KEY,
  item_id INTEGER NOT NULL UNIQUE REFERENCES items(id) ON DELETE CASCADE,
  content_json TEXT NOT NULL,
  markdown TEXT NOT NULL,
  provider TEXT,
  model TEXT,
  is_mock INTEGER NOT NULL DEFAULT 0,
  effort TEXT,
  -- 解读依据：fulltext（Europe PMC OA 全文）| abstract（仅摘要）
  basis TEXT,
  created_at TEXT NOT NULL
);

-- OA 全文（Europe PMC JATS 抽取）。status=ok 才有 text；其余状态只记录原因，
-- 界面据此说明「仅摘要解读」是为什么（非 OA / 无 PMCID / 抓取失败）。
CREATE TABLE IF NOT EXISTS fulltexts (
  item_id INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
  status TEXT NOT NULL DEFAULT 'ok',
  pmcid TEXT,
  license TEXT,
  source TEXT,
  chars INTEGER NOT NULL DEFAULT 0,
  text TEXT NOT NULL DEFAULT '',
  error TEXT,
  fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fulltexts_status ON fulltexts(status);

CREATE TABLE IF NOT EXISTS stories (
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  summary TEXT,
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  heat REAL NOT NULL DEFAULT 0,
  item_count INTEGER NOT NULL DEFAULT 0,
  source_count INTEGER NOT NULL DEFAULT 0,
  primary_topic TEXT,
  best_item_id INTEGER,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stories_heat ON stories(heat);
CREATE INDEX IF NOT EXISTS idx_stories_last_seen ON stories(last_seen);

CREATE TABLE IF NOT EXISTS editions (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  date_key TEXT NOT NULL,
  title TEXT NOT NULL,
  overview TEXT,
  content_json TEXT NOT NULL,
  markdown TEXT NOT NULL,
  provider TEXT,
  model TEXT,
  is_mock INTEGER NOT NULL DEFAULT 0,
  effort TEXT,
  item_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  pushed INTEGER NOT NULL DEFAULT 0,
  UNIQUE(kind, date_key)
);

CREATE TABLE IF NOT EXISTS reading (
  item_id INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
  status TEXT NOT NULL DEFAULT 'unread',
  starred INTEGER NOT NULL DEFAULT 0,
  read_later INTEGER NOT NULL DEFAULT 0,
  rating INTEGER,
  notes TEXT NOT NULL DEFAULT '',
  tags TEXT NOT NULL DEFAULT '[]',
  read_at TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS collections (
  id INTEGER PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS collection_items (
  collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
  item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  added_at TEXT NOT NULL,
  PRIMARY KEY (collection_id, item_id)
);

CREATE TABLE IF NOT EXISTS pushes (
  id INTEGER PRIMARY KEY,
  rule_key TEXT,
  channel_key TEXT NOT NULL,
  event TEXT NOT NULL,
  ref_type TEXT NOT NULL,
  ref_id INTEGER,
  status TEXT NOT NULL,
  error TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL DEFAULT 'running',
  summary TEXT NOT NULL DEFAULT '{}',
  log TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS llm_calls (
  id INTEGER PRIMARY KEY,
  task TEXT NOT NULL,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  is_mock INTEGER NOT NULL DEFAULT 0,
  effort TEXT,
  prompt_tokens INTEGER,
  completion_tokens INTEGER,
  reasoning_tokens INTEGER,
  latency_ms INTEGER,
  cached INTEGER NOT NULL DEFAULT 0,
  ok INTEGER NOT NULL DEFAULT 1,
  error TEXT,
  ref_type TEXT,
  ref_id INTEGER,
  created_at TEXT NOT NULL
);

-- 期刊表：CSV 导入的 IF/等级覆盖（yaml 为主表，这里是增量与覆盖）
CREATE TABLE IF NOT EXISTS journals (
  key TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  aliases TEXT NOT NULL DEFAULT '',
  issn TEXT NOT NULL DEFAULT '',
  tier TEXT NOT NULL DEFAULT '',
  if_value REAL,
  if_year TEXT,
  source TEXT NOT NULL DEFAULT 'import',
  updated_at TEXT NOT NULL
);

-- 真实模型输出缓存：同一任务 + 同一提示词不再重复调用（模拟输出永不缓存）
CREATE TABLE IF NOT EXISTS llm_cache (
  key TEXT PRIMARY KEY,
  task TEXT NOT NULL,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  effort TEXT,
  text TEXT NOT NULL,
  prompt_tokens INTEGER,
  completion_tokens INTEGER,
  hits INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  last_hit_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_llm_cache_task ON llm_cache(task);

CREATE TABLE IF NOT EXISTS kv (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- 全文检索：trigram 分词同时支持中英文子串
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
  title, title_zh, summary, summary_zh, authors, journal, keywords,
  content='items', content_rowid='id', tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS items_ai AFTER INSERT ON items BEGIN
  INSERT INTO items_fts(rowid, title, title_zh, summary, summary_zh, authors, journal, keywords)
  VALUES (new.id, new.title, new.title_zh, new.summary, new.summary_zh, new.authors, new.journal, new.keywords);
END;
CREATE TRIGGER IF NOT EXISTS items_ad AFTER DELETE ON items BEGIN
  INSERT INTO items_fts(items_fts, rowid, title, title_zh, summary, summary_zh, authors, journal, keywords)
  VALUES ('delete', old.id, old.title, old.title_zh, old.summary, old.summary_zh, old.authors, old.journal, old.keywords);
END;
CREATE TRIGGER IF NOT EXISTS items_au AFTER UPDATE ON items BEGIN
  INSERT INTO items_fts(items_fts, rowid, title, title_zh, summary, summary_zh, authors, journal, keywords)
  VALUES ('delete', old.id, old.title, old.title_zh, old.summary, old.summary_zh, old.authors, old.journal, old.keywords);
  INSERT INTO items_fts(rowid, title, title_zh, summary, summary_zh, authors, journal, keywords)
  VALUES (new.id, new.title, new.title_zh, new.summary, new.summary_zh, new.authors, new.journal, new.keywords);
END;
