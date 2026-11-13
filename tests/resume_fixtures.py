"""Synthetic senior-engineering resumes for the export-format tests. Nothing here is real data."""

from __future__ import annotations


def _bullet(job: int, n: int, long: bool = False) -> str:
    text = (
        f"Delivered service {job}.{n} that cut p95 latency by {10 + n}% across 3 regions and kept error "
        f"budgets under 0.{n + 1}%, working with product, security and platform teams."
    )
    if long:
        text += " Mentored four engineers, wrote the design doc, ran the migration and owned the on-call rotation."
    return text


def make_resume(bullets_per_job: tuple[int, ...], *, long: bool = False) -> dict:
    companies = [
        ("Northwind Distributed Systems International", "Austin, TX", "Jun 2023 - Present", "Senior Staff Software Engineer"),
        ("Contoso Cloud Platform Services", "Seattle, WA", "Mar 2020 - May 2023", "Senior Software Engineer"),
        ("Fabrikam Data Infrastructure Group", "Remote", "Jan 2017 - Feb 2020", "Software Engineer"),
        ("Tailspin Toys", "Denver, CO", "Aug 2014 - Dec 2016", "Junior Software Engineer"),
    ]
    jobs = []
    for index, count in enumerate(bullets_per_job):
        company, location, dates, title = companies[index]
        jobs.append({
            "company": company, "location": location, "dates": dates, "title": title,
            "role_description": "Owned the reliability and delivery of the core platform serving enterprise customers.",
            "bullets": [_bullet(index, n, long) for n in range(count)],
        })
    label = "Distributed Systems and Cloud Infrastructure Engineering" if long else "Backend"
    return {
        "name": "Test Person", "subtitle": "Senior Software Engineer", "location": "Remote, US",
        "email": "test.person@example.com", "phone": "(555) 010-0000",
        "summary": "Senior engineer who builds and runs large distributed systems. " * 3,
        "experience": jobs,
        "skill_groups": [
            {"category": label, "items": ["Python", "Go", "Java", "PostgreSQL", "Kafka", "gRPC"]},
            {"category": "Cloud", "items": ["AWS", "Kubernetes", "Terraform", "Docker"]},
        ],
        "additional_categories": [
            {"category": "Leadership", "items": ["Mentoring", "Design reviews", "Incident command"]},
        ],
        "education": [{"school": "State University", "location": "Springfield, IL", "degree": "B.S. Computer Science"}],
    }


FIXTURES = {
    "14 bullets": make_resume((5, 5, 4)),
    "18 bullets": make_resume((6, 6, 4, 2)),
    "22 bullets, long labels": make_resume((6, 6, 6, 4), long=True),
}
