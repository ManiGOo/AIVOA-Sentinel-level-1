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

# 2. Database engine using sync psycopg2
DB_URL = os.getenv("DATABASE_URL", "postgresql://pharmabkp:aivoadma25@216.48.184.249:5432/pharma")

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

class RegulatoryEvidence(Base):
    """External regulatory evidence (FDA warning letters, EudraGMDP
    non-compliance statements) used as Layer-1 paper-QMS detection."""
    __tablename__ = 'regulatory_evidence'

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source = Column(String(50))          # 'FDA' | 'EudraGMDP'
    firm_name = Column(Text)             # as shown by the source
    mfr_key = Column(Text, index=True)   # normalized key (matches regulatory_events)
    finding_date = Column(Date, nullable=True)
    url = Column(Text)
    evidence_text = Column(Text)
    classification = Column(JSONB, nullable=True)  # LLM paper-QMS verdict
    paper_qms_score = Column(Integer, default=0)
    evidence_quote = Column(Text, default='')
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
        "ALTER TABLE sdr_data.regulatory_evidence "
        "ADD COLUMN IF NOT EXISTS mfr_key TEXT",
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
