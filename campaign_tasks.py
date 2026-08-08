import os
import asyncio
import json
import re
import uuid
from datetime import timedelta, datetime
from temporalio import activity, workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from db_setup import SessionLocal, Campaign, CampaignLead, CompanyLead
    from cognitive_engine import client as groq_client, GROQ_API_KEY


# ---------------------------------------------------------------------------
# Message personalization
# ---------------------------------------------------------------------------

def _generate_message(company_name: str, decision_maker: dict, context: dict,
                      step: int, channel: str, sequence_config: dict) -> dict:
    """Use Groq to generate a personalized message based on lead triggers."""
    if not GROQ_API_KEY.startswith("gsk_"):
        return _heuristic_message(company_name, decision_maker, context, step, channel)

    dm_name = decision_maker.get("name", "").split()[0] if decision_maker.get("name") else ""
    dm_role = decision_maker.get("role", "")
    triggers = context.get("trigger_events", [])
    hiring = [s for s in context.get("intent_signals", []) if s.get("category") == "hiring"]
    trigger_summary = "; ".join(t.get("title", "") for t in triggers[:3])
    hiring_summary = "; ".join(h.get("title", "") for h in hiring[:2])

    step_names = {0: "first outreach", 1: "LinkedIn touch", 2: "follow-up", 3: "breakup"}
    step_name = step_names.get(step, f"touch {step + 1}")

    prompt = f"""Write a short, personalized {channel} message for {step_name} in a QMS software outreach campaign.

Recipient: {dm_name} ({dm_role}) at {company_name}
Company trigger events: {trigger_summary or "none available"}
Company hiring: {hiring_summary or "none available"}

Rules:
- Reference a specific trigger event or hiring signal to show this isn't generic
- Keep it under 3 sentences for email body, 1 sentence for LinkedIn
- No pitch-slap — lead with insight or empathy, not a product demo
- Sound like a human salesperson, not a marketing bot
- If email, provide a subject line

Respond ONLY with JSON: {{"subject": "...", "body": "..."}}
"""
    try:
        completion = groq_client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=[
                {"role": "system", "content": "You write strict JSON."},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.4,
            max_tokens=300,
        )
        data = json.loads(completion.choices[0].message.content)
        return {"subject": data.get("subject", ""), "body": data.get("body", "")}
    except Exception as e:
        print(f"Groq message generation error: {e}")
        return _heuristic_message(company_name, decision_maker, context, step, channel)


def _heuristic_message(company_name: str, decision_maker: dict, context: dict,
                       step: int, channel: str) -> dict:
    dm_name = decision_maker.get("name", "").split()[0] if decision_maker.get("name") else "there"
    triggers = context.get("trigger_events", [])
    trigger_title = triggers[0].get("title", "") if triggers else ""

    if step == 0:
        subject = f"QMS for {company_name}"
        body = f"Hi {dm_name}, I noticed {company_name} recently had {trigger_title or 'some quality signals'}. With the right QMS, those dissolution failures can be caught before inspection. Worth a quick call?"
    elif step == 1:
        subject = ""
        body = f"Hi {dm_name}, came across your profile — noticed {company_name} is dealing with {trigger_title or 'some regulatory signals'}. Would love to share how similar pharma teams handle it."
    elif step == 2:
        subject = f"Following up — {company_name}"
        body = f"Hi {dm_name}, just following up. Given the recent {trigger_title or 'quality events'} at {company_name}, I think a 10-min chat could help. Let me know if you're open to it."
    else:
        subject = f"Last try — {company_name}"
        body = f"Hi {dm_name}, I'll leave this here. If {company_name} ever wants to get ahead of CDSCO inspections with better quality processes, happy to help. No pressure."

    return {"subject": subject, "body": body}


# ---------------------------------------------------------------------------
# Activities
# ---------------------------------------------------------------------------

@activity.defn
def create_campaign_activity(data: dict) -> dict:
    """Create a campaign and its campaign_lead entries from selected leads."""
    db = SessionLocal()
    try:
        campaign_id = data.get("campaign_id") or f"campaign-{uuid.uuid4().hex[:12]}"
        campaign = Campaign(
            campaign_id=campaign_id,
            name=data.get("name", "Untitled Campaign"),
            status="draft",
            sequence_config=data.get("sequence_config", []),
            created_by=data.get("created_by", ""),
        )
        db.add(campaign)

        for lead_ref in data.get("leads", []):
            company_key = lead_ref.get("company_key")
            lead_data = db.query(CompanyLead).filter(CompanyLead.company_key == company_key).first()
            dm = lead_ref.get("decision_maker", {})
            if not dm and lead_data:
                verified = [d for d in (lead_data.decision_makers or []) if d.get("confidence") == "high"]
                dm = verified[0] if verified else (lead_data.decision_makers or [{}])[0] if lead_data.decision_makers else {}

            context = {}
            if lead_data:
                context = {
                    "trigger_events": lead_data.trigger_events or [],
                    "intent_signals": lead_data.intent_signals or [],
                    "company_status": lead_data.company_status,
                }

            cl = CampaignLead(
                campaign_id=campaign_id,
                company_key=company_key,
                status="pending",
                current_step=0,
                decision_maker=dm,
                lead_context=context,
            )
            db.add(cl)

        db.commit()
        lead_count = len(data.get("leads", []))
        return {"campaign_id": campaign_id, "status": "draft", "lead_count": lead_count}
    except Exception as e:
        db.rollback()
        print(f"create_campaign_activity error: {e}")
        return {"campaign_id": data.get("campaign_id", ""), "status": "failed", "error": str(e)[:500]}
    finally:
        db.close()


@activity.defn
def start_campaign_activity(campaign_id: str) -> dict:
    """Mark campaign as running."""
    db = SessionLocal()
    try:
        campaign = db.query(Campaign).filter(Campaign.campaign_id == campaign_id).first()
        if campaign:
            campaign.status = "running"
            campaign.started_at = datetime.utcnow()
            db.commit()
        return {"campaign_id": campaign_id, "status": "running"}
    except Exception as e:
        db.rollback()
        return {"campaign_id": campaign_id, "status": "failed"}
    finally:
        db.close()


@activity.defn
def generate_lead_messages_activity(campaign_id: str, company_key: str) -> dict:
    """Generate personalized messages for all steps of a campaign lead."""
    db = SessionLocal()
    try:
        campaign = db.query(Campaign).filter(Campaign.campaign_id == campaign_id).first()
        cl = db.query(CampaignLead).filter(
            CampaignLead.campaign_id == campaign_id,
            CampaignLead.company_key == company_key,
        ).first()
        if not campaign or not cl:
            return {"campaign_id": campaign_id, "company_key": company_key, "messages": []}

        lead_data = db.query(CompanyLead).filter(CompanyLead.company_key == company_key).first()
        company_name = lead_data.company_name if lead_data else company_key

        messages = []
        for idx, step in enumerate(campaign.sequence_config or []):
            msg = _generate_message(
                company_name, cl.decision_maker or {},
                cl.lead_context or {}, idx,
                step.get("channel", "email"), step,
            )
            messages.append({
                "step": idx,
                "channel": step.get("channel", "email"),
                "subject": msg.get("subject", ""),
                "body": msg.get("body", ""),
                "status": "draft",
            })

        cl.messages = messages
        cl.status = "ready"
        db.commit()
        return {"campaign_id": campaign_id, "company_key": company_key, "messages": messages}
    except Exception as e:
        db.rollback()
        print(f"generate_lead_messages_activity error: {e}")
        return {"campaign_id": campaign_id, "company_key": company_key, "messages": []}
    finally:
        db.close()


@activity.defn
def send_message_activity(campaign_id: str, company_key: str, step: int) -> dict:
    """Record a message as sent (in a real system this would call an email/LinkedIn API)."""
    db = SessionLocal()
    try:
        cl = db.query(CampaignLead).filter(
            CampaignLead.campaign_id == campaign_id,
            CampaignLead.company_key == company_key,
        ).first()
        if not cl or not cl.messages or step >= len(cl.messages):
            return {"campaign_id": campaign_id, "company_key": company_key, "sent": False}

        cl.messages[step]["status"] = "sent"
        cl.messages[step]["sent_at"] = datetime.utcnow().isoformat()
        cl.status = "contacted"
        cl.current_step = step
        cl.last_contact_at = datetime.utcnow()
        db.commit()
        return {"campaign_id": campaign_id, "company_key": company_key, "sent": True, "step": step}
    except Exception as e:
        db.rollback()
        return {"campaign_id": campaign_id, "company_key": company_key, "sent": False}
    finally:
        db.close()


@activity.defn
def complete_campaign_activity(campaign_id: str) -> dict:
    db = SessionLocal()
    try:
        campaign = db.query(Campaign).filter(Campaign.campaign_id == campaign_id).first()
        if campaign:
            campaign.status = "completed"
            campaign.completed_at = datetime.utcnow()
            db.commit()
        return {"campaign_id": campaign_id, "status": "completed"}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------

@workflow.defn
class CampaignWorkflow:
    def __init__(self):
        self._status = "starting"

    @workflow.query
    def progress(self) -> dict:
        return {"status": self._status}

    @workflow.run
    async def run(self, campaign_id: str) -> dict:
        self._status = "starting"
        await workflow.execute_activity(
            start_campaign_activity, args=[campaign_id],
            start_to_close_timeout=timedelta(minutes=1),
        )

        self._status = "generating_messages"
        db = SessionLocal()
        try:
            campaign = db.query(Campaign).filter(Campaign.campaign_id == campaign_id).first()
            leads = db.query(CampaignLead).filter(CampaignLead.campaign_id == campaign_id).all()
        finally:
            db.close()

        for cl in leads:
            await workflow.execute_activity(
                generate_lead_messages_activity,
                args=[campaign_id, cl.company_key],
                start_to_close_timeout=timedelta(minutes=3),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )

        self._status = "sending_sequence"
        for step_idx, step in enumerate(campaign.sequence_config or []):
            for cl in leads:
                if cl.status in ("replied", "not_interested"):
                    continue
                await workflow.execute_activity(
                    send_message_activity,
                    args=[campaign_id, cl.company_key, step_idx],
                    start_to_close_timeout=timedelta(minutes=1),
                )
            delay = step.get("delay_days", 0)
            if delay > 0 and step_idx < len(campaign.sequence_config) - 1:
                await workflow.sleep(timedelta(days=delay))

        self._status = "completed"
        await workflow.execute_activity(
            complete_campaign_activity, args=[campaign_id],
            start_to_close_timeout=timedelta(minutes=1),
        )
        return {"campaign_id": campaign_id, "status": "completed"}
