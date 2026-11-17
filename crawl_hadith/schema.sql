-- SQLite schema for the Inoor hadith crawler.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS metadata (
    meta_key   TEXT PRIMARY KEY,
    meta_value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    id            INTEGER PRIMARY KEY,
    main_title    TEXT,
    title         TEXT,
    group_title   TEXT,
    order_num     INTEGER,
    sample_word   TEXT,
    author_title  TEXT,
    hadith_count  INTEGER,
    century_id    INTEGER
);

CREATE TABLE IF NOT EXISTS translation_sources (
    id           INTEGER PRIMARY KEY,
    title        TEXT,
    short_title  TEXT,
    author_title TEXT
);

CREATE TABLE IF NOT EXISTS explanation_sources (
    id           INTEGER PRIMARY KEY,
    title        TEXT,
    short_title  TEXT,
    author_title TEXT
);

CREATE TABLE IF NOT EXISTS formats (
    id           INTEGER PRIMARY KEY,
    title        TEXT,
    tag_name     TEXT,
    access_level INTEGER,
    order_num    INTEGER
);

CREATE TABLE IF NOT EXISTS languages (
    id               INTEGER PRIMARY KEY,
    title            TEXT,
    native_name      TEXT,
    two_char         TEXT,
    is_active        INTEGER,
    is_rtl           INTEGER,
    order_num        INTEGER,
    is_for_data      INTEGER,
    is_for_interface INTEGER
);

CREATE TABLE IF NOT EXISTS qael_roles (
    id    INTEGER PRIMARY KEY,
    title TEXT
);

CREATE TABLE IF NOT EXISTS qael (
    id    INTEGER PRIMARY KEY,
    title TEXT
);

CREATE TABLE IF NOT EXISTS tocs (
    id           INTEGER PRIMARY KEY,
    source_id    INTEGER REFERENCES sources(id),
    parent_id    INTEGER,
    title        TEXT,
    sample_title TEXT,
    access_level INTEGER,
    level_num    INTEGER,
    hadith_count INTEGER,
    child_count  INTEGER,
    is_toc       INTEGER,
    sort_order   INTEGER
);

CREATE TABLE IF NOT EXISTS hadiths (
    hadith_id            INTEGER PRIMARY KEY,
    source_id            INTEGER REFERENCES sources(id),
    author_title         TEXT,
    book_title           TEXT,
    book_title_short     TEXT,
    vol                  INTEGER,
    page_num             INTEGER,
    start_index_in_page  INTEGER,
    access_level         INTEGER,
    text                 TEXT,
    text_plain           TEXT,
    short_text           TEXT,
    text_no_erab         TEXT,
    qael_title_list      TEXT,
    toc_text             TEXT,
    translate_count      INTEGER,
    explanation_count    INTEGER,
    hadith_in_book_count INTEGER,
    hadith_related_count INTEGER,
    hadith_same_toc_count INTEGER,
    group_hadith_count   INTEGER,
    has_translate        INTEGER,
    has_explanation      INTEGER,
    has_similar          INTEGER,
    has_family           INTEGER,
    noor_lib_link        TEXT
);

CREATE TABLE IF NOT EXISTS hadith_group_members (
    group_id           INTEGER NOT NULL,
    hadith_id          INTEGER NOT NULL,
    source_id          INTEGER REFERENCES sources(id),
    source_main_title  TEXT,
    source_short_title TEXT,
    source_hadith_id   INTEGER,
    vol                INTEGER,
    page_num           INTEGER,
    century_id         INTEGER,
    access_level       INTEGER,
    section            INTEGER,
    group_hadith_count INTEGER,
    size_id            INTEGER,
    size_title         TEXT,
    size_json          TEXT,
    book_json          TEXT,
    sort_order         INTEGER,
    PRIMARY KEY (group_id, hadith_id)
);

CREATE TABLE IF NOT EXISTS hadith_qael (
    hadith_id INTEGER NOT NULL REFERENCES hadiths(hadith_id) ON DELETE CASCADE,
    qael_id   INTEGER NOT NULL REFERENCES qael(id),
    role_id   INTEGER NOT NULL REFERENCES qael_roles(id),
    sort_order INTEGER,
    PRIMARY KEY (hadith_id, qael_id, role_id)
);

CREATE TABLE IF NOT EXISTS hadith_toc (
    hadith_id INTEGER NOT NULL REFERENCES hadiths(hadith_id) ON DELETE CASCADE,
    toc_id    INTEGER NOT NULL REFERENCES tocs(id),
    depth     INTEGER NOT NULL,
    PRIMARY KEY (hadith_id, toc_id)
);

CREATE TABLE IF NOT EXISTS translations (
    id            INTEGER PRIMARY KEY,
    hadith_id     INTEGER NOT NULL REFERENCES hadiths(hadith_id) ON DELETE CASCADE,
    source_id     INTEGER REFERENCES translation_sources(id),
    language_id   INTEGER REFERENCES languages(id),
    text          TEXT,
    text_plain    TEXT,
    short_text    TEXT,
    is_shorted    INTEGER,
    vol           INTEGER,
    page_num      INTEGER,
    comment_count INTEGER,
    insert_date   TEXT,
    noor_lib_link TEXT
);

CREATE TABLE IF NOT EXISTS explanations (
    id            INTEGER PRIMARY KEY,
    hadith_id     INTEGER NOT NULL REFERENCES hadiths(hadith_id) ON DELETE CASCADE,
    source_id     INTEGER REFERENCES explanation_sources(id),
    language_id   INTEGER REFERENCES languages(id),
    text          TEXT,
    text_plain    TEXT,
    short_text    TEXT,
    is_shorted    INTEGER,
    vol           INTEGER,
    page_num      INTEGER,
    comment_count INTEGER,
    insert_date   TEXT,
    noor_lib_link TEXT
);

CREATE TABLE IF NOT EXISTS ravis (
    ravi_id   INTEGER PRIMARY KEY,
    name      TEXT,
    hint      TEXT,
    info_json TEXT
);

CREATE TABLE IF NOT EXISTS hadith_sanad (
    hadith_id     INTEGER NOT NULL REFERENCES hadiths(hadith_id) ON DELETE CASCADE,
    sanad_index   INTEGER NOT NULL,
    segment_order INTEGER NOT NULL,
    segment_type  INTEGER,
    title         TEXT,
    PRIMARY KEY (hadith_id, sanad_index, segment_order)
);

CREATE TABLE IF NOT EXISTS hadith_sanad_ravi (
    hadith_id     INTEGER NOT NULL,
    sanad_index   INTEGER NOT NULL,
    segment_order INTEGER NOT NULL,
    ravi_index    INTEGER NOT NULL,
    ravi_id       INTEGER NOT NULL REFERENCES ravis(ravi_id),
    hint          TEXT,
    PRIMARY KEY (hadith_id, sanad_index, segment_order, ravi_index),
    FOREIGN KEY (hadith_id, sanad_index, segment_order)
        REFERENCES hadith_sanad(hadith_id, sanad_index, segment_order)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS crawl_status (
    hadith_id         INTEGER PRIMARY KEY,
    main_state        TEXT NOT NULL DEFAULT 'pending'
        CHECK (main_state IN ('pending','done','fallback','failed','skipped','missing')),
    translate_state   TEXT NOT NULL DEFAULT 'pending'
        CHECK (translate_state IN ('pending','done','fallback','failed','skipped','missing')),
    explanation_state TEXT NOT NULL DEFAULT 'pending'
        CHECK (explanation_state IN ('pending','done','fallback','failed','skipped','missing')),
    rejal_state       TEXT NOT NULL DEFAULT 'pending'
        CHECK (rejal_state IN ('pending','done','fallback','failed','skipped','missing')),
    attempts          INTEGER NOT NULL DEFAULT 0,
    last_error        TEXT,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_seed (
    source_id  INTEGER PRIMARY KEY REFERENCES sources(id) ON DELETE CASCADE,
    status     TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','done','failed')),
    next_page  INTEGER NOT NULL DEFAULT 1,
    attempts   INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_hadiths_source_page ON hadiths(source_id, vol, page_num);
CREATE INDEX IF NOT EXISTS idx_hgm_group ON hadith_group_members(group_id);
CREATE INDEX IF NOT EXISTS idx_hgm_hadith ON hadith_group_members(hadith_id);
CREATE INDEX IF NOT EXISTS idx_hadith_qael_qael ON hadith_qael(qael_id);
CREATE INDEX IF NOT EXISTS idx_hadith_toc_toc ON hadith_toc(toc_id, depth);
CREATE INDEX IF NOT EXISTS idx_translations_hadith ON translations(hadith_id);
CREATE INDEX IF NOT EXISTS idx_explanations_hadith ON explanations(hadith_id);
CREATE INDEX IF NOT EXISTS idx_sanad_ravi_ravi ON hadith_sanad_ravi(ravi_id);
CREATE INDEX IF NOT EXISTS idx_crawl_main ON crawl_status(main_state);
CREATE INDEX IF NOT EXISTS idx_crawl_translate ON crawl_status(translate_state);
CREATE INDEX IF NOT EXISTS idx_crawl_explanation ON crawl_status(explanation_state);
CREATE INDEX IF NOT EXISTS idx_crawl_rejal ON crawl_status(rejal_state);

-- Same-chapter ("احادیث هم باب") relationships are derived from the leaf toc
-- recorded on each hadith. These views keep that derivation in one place.
CREATE VIEW IF NOT EXISTS hadith_leaf_toc AS
SELECT hadith_id, toc_id, depth
FROM hadith_toc ht
WHERE depth = (
    SELECT MAX(depth) FROM hadith_toc WHERE hadith_id = ht.hadith_id
);

CREATE VIEW IF NOT EXISTS v_same_toc_hadith AS
SELECT a.hadith_id AS hadith_id,
       b.hadith_id AS same_toc_hadith_id,
       a.toc_id    AS toc_id
FROM hadith_leaf_toc a
JOIN hadith_leaf_toc b ON b.toc_id = a.toc_id
WHERE a.hadith_id <> b.hadith_id;
