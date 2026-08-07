import os
import uuid
from datetime import datetime
from sqlalchemy import create_engine, Column, String, Integer, Date, Boolean, MetaData, Text, text
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.types import DateTime

# 1. Define metadata with the explicit schema
sdr_metadata = MetaData(schema='sdr_data')
Base = declarative_base(metadata=sdr_metadata)

# 2. Database engine using sync psycopg2. The URL must come from the
# environment (DATABASE_URL). The credentials used to live hardcoded here as a
# fallback — that password is now considered compromised (it is in git
# history) and must be provided via env only.
DB_URL = os.getenv("DATABASE_URL")
if not DB_URL:
    raise RuntimeError(
        "DATABASE_URL is not set. Set it in the environment (or .env) to "
        "postgresql://<user>:<pass>@<host>:5432/<dbname>.")

# If user provided a postgresql+asyncpg URL, let's normalize it for sync psycopg2
if DB_URL.startswith("postgresql+asyncpg://"):
    DB_URL = DB_URL.replace("postgresql+asyncpg://", "postgresql://")

engine = create_engine(DB_URL)
SessionLocal = sessionmaker(bind=engine)

class RegulatoryEvent(Base):
    __tablename__ = 'regulatory_events'
    
    event_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id = Column(UUID(as_uuid=True), nullable=True) # Populated later via Entity Resolution
    regulator = Column(String(50), default="CDSCO")
    event_type = Column(String(100)) # 'NSQ_DRUG' or 'SPURIOUS_DRUG'
    
    raw_details = Column(JSONB)      # The raw JSON from CDSCO
    llm_analysis = Column(JSONB, nullable=True) # The JSON output from Groq/Qwen
    score = Column(Integer, default=0)
    reporting_source = Column(Text, nullable=True, server_default='')
    reported_by = Column(Text, nullable=True, server_default='')
    
    event_date = Column(Date, default=datetime.utcnow)

    paper_evidence_class = Column(String(20), default='')  # '' | explicit | deductive | none
    paper_confidence = Column(Integer, default=0)          # 0-100
    paper_proxies = Column(JSONB, default=list)            # satisfied proxy labels

class RegulatoryEvidence(Base):
    """External regulatory evidence (FDA warning letters, EudraGMDP
    non-compliance statements) used as Layer-1 paper-QMS detection."""
    __tablename__ = 'regulatory_evidence'

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source = Column(String(50))          # 'FDA' | 'EudraGMDP'
    firm_name = Column(Text)             # as shown by the source
    mfr_key = Column(Text, index=True)   # normalized key (matches regulatory_events)
    company_key = Column(Text, index=True)  # cleaned company name key (entity-level)
    finding_date = Column(Date, nullable=True)
    url = Column(Text)
    evidence_text = Column(Text)
    classification = Column(JSONB, nullable=True)  # LLM paper-QMS verdict
    paper_qms_score = Column(Integer, default=0)
    evidence_quote = Column(Text, default='')
    fetched_at = Column(DateTime, default=datetime.utcnow)

class EnrichmentCheck(Base):
    """Outcome of an enrichment run for one manufacturer + source — recorded
    even when no findings are found, so cards can show 'checked, no findings'."""
    __tablename__ = 'enrichment_checks'

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    mfr_key = Column(Text, index=True)       # normalized key (matches regulatory_events)
    company_key = Column(Text, index=True)   # cleaned company name key (entity-level)
    source = Column(String(50))              # 'FDA' | 'EudraGMDP'
    searched_name = Column(Text, default='') # cleaned name actually queried
    findings_count = Column(Integer, default=0)
    inserted_count = Column(Integer, default=0)
    paper_qms_count = Column(Integer, default=0)
    status = Column(String(20), default='completed')  # completed | error
    error = Column(Text, default='')
    checked_at = Column(DateTime, default=datetime.utcnow)

class WebEvidence(Base):
    """Web evidence found by the agent for a specific manufacturer/event."""
    __tablename__ = 'web_evidence'

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id = Column(UUID(as_uuid=True), nullable=True) # link to the specific record
    mfr_key = Column(Text, index=True) # normalized manufacturer
    query = Column(Text)               # the exact search query used
    title = Column(Text)               # article headline
    url = Column(Text)                 # canonical article URL
    source = Column(Text)              # domain / outlet
    published_date = Column(Date, nullable=True)
    snippet = Column(Text)             # search-engine snippet
    full_text = Column(Text)           # fetched article body
    classification = Column(JSONB, nullable=True) # LLM verdict
    relevance_score = Column(Integer, default=0)
    fetch_status = Column(Text)        # meta / fetched / blocked / failed
    fetched_at = Column(DateTime, default=datetime.utcnow)


class CompanyLead(Base):
    """Lead research outcome for one company: website, LinkedIn page and
    hiring signal (current job postings + hiring-news mentions)."""
    __tablename__ = 'company_leads'

    company_key = Column(String(255), primary_key=True)
    company_name = Column(Text, default='')
    website = Column(Text, default='')
    linkedin_url = Column(Text, default='')
    hiring = Column(JSONB, default=list)          # [{title, location, posted, url}]
    hiring_news = Column(JSONB, default=list)     # [{title, url, source, snippet, date}]
    summary = Column(JSONB, default=dict)         # raw research/notes
    status = Column(String(20), default='not_started')  # not_started | running | completed | failed
    error = Column(Text, default='')
    workflow_id = Column(Text, default='')
    fetched_at = Column(DateTime, default=datetime.utcnow)


def init_db():
    # 3. Create the schema if it doesn't exist using a raw connection
    with engine.connect() as conn:
        conn.execute(text("CREATE SCHEMA IF NOT EXISTS sdr_data"))
        conn.commit()
        
    # 4. Create the tables
    Base.metadata.create_all(engine)

    # 5. Safe migrations for tables that may already exist
    migrations = [
        "ALTER TABLE sdr_data.regulatory_events "
        "ADD COLUMN IF NOT EXISTS reporting_source VARCHAR(50) DEFAULT ''",
        "ALTER TABLE sdr_data.regulatory_events "
        "ADD COLUMN IF NOT EXISTS reported_by VARCHAR(100) DEFAULT ''",
        "ALTER TABLE sdr_data.regulatory_events ALTER COLUMN reporting_source TYPE TEXT",
        "ALTER TABLE sdr_data.regulatory_events ALTER COLUMN reported_by TYPE TEXT",
        "ALTER TABLE sdr_data.regulatory_events "
        "ADD COLUMN IF NOT EXISTS paper_evidence_class VARCHAR(20) DEFAULT ''",
        "ALTER TABLE sdr_data.regulatory_events "
        "ADD COLUMN IF NOT EXISTS paper_confidence INT DEFAULT 0",
        "ALTER TABLE sdr_data.regulatory_events "
        "ADD COLUMN IF NOT EXISTS paper_proxies JSONB DEFAULT '[]'",
        "CREATE INDEX IF NOT EXISTS ix_events_paper_class "
        "ON sdr_data.regulatory_events (paper_evidence_class)",
        "ALTER TABLE sdr_data.regulatory_evidence "
        "ADD COLUMN IF NOT EXISTS mfr_key TEXT",
        "ALTER TABLE sdr_data.regulatory_evidence "
        "ADD COLUMN IF NOT EXISTS company_key TEXT",
        "ALTER TABLE sdr_data.enrichment_checks "
        "ADD COLUMN IF NOT EXISTS company_key TEXT",
        "CREATE INDEX IF NOT EXISTS ix_evidence_company_key "
        "ON sdr_data.regulatory_evidence (company_key)",
        "CREATE INDEX IF NOT EXISTS ix_checks_company_key "
        "ON sdr_data.enrichment_checks (company_key)",
        "CREATE INDEX IF NOT EXISTS ix_web_evidence_mfr_key "
        "ON sdr_data.web_evidence (mfr_key)",
    ]
    for sql in migrations:
        with engine.connect() as conn:
            try:
                conn.execute(text(sql))
                conn.commit()
            except Exception:
                conn.rollback()

    print("Database schema 'sdr_data' and tables created successfully.")

if __name__ == "__main__":
    init_db()
