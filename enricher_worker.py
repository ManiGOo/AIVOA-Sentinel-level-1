import asyncio
from concurrent.futures import ThreadPoolExecutor

from db_setup import init_db
from temporal_connect import connect_with_retry
from temporalio.worker import Worker
from enrichment_tasks import EnrichmentWorkflow, fetch_external_evidence
from web_evidence_tasks import (
    WebEvidenceWorkflow,
    generate_queries_activity,
    search_web_for_queries,
    fetch_and_classify_articles,
)
from lead_research_tasks import (
    LeadResearchWorkflow,
    search_company_profile_activity,
    search_decision_makers_activity,
    search_intent_signals_activity,
    extract_people_activity,
    evaluate_and_save_lead_activity,
    mark_lead_failed_activity,
    scrape_company_website_activity,
    search_corporate_registry_activity,
)
from regulatory_scrape_tasks import (
    RegulatoryFullPullWorkflow,
    ScrapedRecordCheckWorkflow,
    scrape_regulatory_records,
    save_scraped_records,
    link_scraped_records_for_firm,
)
from fda_eu_scrape_tasks import (
    FDAEScraperWorkflow,
    FDAEEnrichmentWorkflow,
    scrape_fda_e_records,
    save_fda_e_raw,
    load_fda_e_enrichment_candidates,
    apply_fda_e_enrichment_to_db,
)


async def main():
    init_db()
    client = await connect_with_retry()
    worker = Worker(
        client,
        task_queue="enrichment-task-queue",
        workflows=[EnrichmentWorkflow, WebEvidenceWorkflow, LeadResearchWorkflow,
                   RegulatoryFullPullWorkflow, ScrapedRecordCheckWorkflow,
                   FDAEScraperWorkflow, FDAEEnrichmentWorkflow],
        activities=[
            fetch_external_evidence,
            generate_queries_activity,
            search_web_for_queries,
            fetch_and_classify_articles,
            search_company_profile_activity,
            search_decision_makers_activity,
            extract_people_activity,
            search_intent_signals_activity,
            evaluate_and_save_lead_activity,
            mark_lead_failed_activity,
            scrape_company_website_activity,
            search_corporate_registry_activity,
            scrape_regulatory_records,
            save_scraped_records,
            link_scraped_records_for_firm,
            scrape_fda_e_records,
            save_fda_e_raw,
            load_fda_e_enrichment_candidates,
            apply_fda_e_enrichment_to_db,
        ],
        activity_executor=ThreadPoolExecutor(max_workers=20),
    )
    print("Starting Temporal Worker for Enrichment on 'enrichment-task-queue'...")
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
