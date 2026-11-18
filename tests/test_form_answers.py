"""What the bot decides to put in each field, and, above all, what it refuses to guess."""

from __future__ import annotations

from datetime import date

import pytest

from core.apply_facts import DECLINE, clean_facts
from core.form_answers import (
    CHAT,
    COVER_FILE,
    FILL,
    NEEDS,
    RESUME_FILE,
    SKIP,
    Context,
    Field,
    Option,
    choose_option,
    classify,
    decide,
)
from core.settings_store import ProfileSettings

PROFILE = ProfileSettings(name="Mary Ann van der Berg", email="mary@example.com", phone="(555) 010-0000",
                          location="Austin, TX", linkedin_url="https://linkedin.com/in/mary",
                          work_authorization="Yes", needs_sponsorship="No")


def ctx(**kw) -> Context:
    facts = {"city": "Austin", "state": "TX", "salary_expectation": "$150,000", "years_experience": "8",
             "notice_period": "2 weeks", "relocation": "No", "over_18": "Yes", "agree_consents": "Yes",
             "referral_source": "LinkedIn", **kw.pop("facts", {})}
    return Context(profile=PROFILE, facts=facts, **kw)


def text(label, required=False, **kw):
    return Field(ref="r", kind=kw.pop("kind", "text"), label=label, required=required, **kw)


def choice(label, labels, required=True, kind="select"):
    return Field(ref="r", kind=kind, label=label, required=required, options=[Option(l, ref=f"o{i}") for i, l in enumerate(labels)])


@pytest.mark.parametrize("label, key", [
    ("First Name", "first_name"), ("Legal first name", "first_name"), ("Last Name", "last_name"),
    ("Email", "email"), ("Phone", "phone"), ("LinkedIn Profile", "linkedin_url"),
    ("Are you legally authorized to work in the United States?", "work_authorization"),
    ("Will you now or in the future require sponsorship?", "needs_sponsorship"),
    ("Are you authorized to work in the US without sponsorship?", "authorized_and_no_sponsorship"),
    ("What are your salary expectations?", "salary_expectation"),
    ("How did you hear about this job?", "referral_source"),
    ("Gender", "eeo_gender"), ("Are you a protected veteran?", "eeo_veteran"),
    ("Disability Status", "eeo_disability"), ("Are you Hispanic/Latino?", "eeo_hispanic"),
    ("Are you willing to relocate?", "relocation"),
    ("How many years of experience do you have?", "years_experience"),
    ("I acknowledge the Privacy Notice", "agree_consents"),
])
def test_labels_map_to_the_fact_they_ask_about(label, key):
    assert classify(label) == key


def test_questions_that_mix_or_narrow_a_fact_are_not_forced_into_it():
    assert classify("Are you authorized to work in the US and will you require sponsorship?") == "ambiguous"
    assert classify("How many years of experience do you have with Python?") == ""  # one skill, not the total


def test_identity_fields_come_from_the_profile():
    c = ctx()
    assert decide(text("First Name", True), c).value == "Mary Ann"
    assert decide(text("Last Name", True), c).value == "van der Berg"
    assert decide(text("Email", True), c).value == "mary@example.com"
    assert decide(text("Location (City)", True), c).value == "Austin, TX"
    d = decide(text("LinkedIn Profile"), c)
    assert (d.action, d.value, d.source) == (FILL, "https://linkedin.com/in/mary", "profile")


def test_a_field_that_already_has_a_value_is_left_alone():
    assert decide(text("Email", True, value="typed@by.me"), ctx()).action == SKIP


def test_yes_no_questions_use_the_profile_and_pick_the_matching_option():
    c = ctx()
    auth = decide(choice("Are you legally authorized to work in the United States?", ["Yes", "No"]), c)
    assert (auth.action, auth.value) == (FILL, "Yes")
    spons = decide(choice("Will you require visa sponsorship?", ["Yes, I will", "No, I will not"], kind="radio"), c)
    assert spons.value == "No, I will not"
    both = decide(choice("Authorized to work without sponsorship?", ["Yes", "No"]), c)
    assert both.value == "Yes"  # authorized and needs none


def test_authorized_but_needing_sponsorship_answers_no_to_the_combined_question():
    needs = Context(profile=ProfileSettings(name="A B", work_authorization="Yes", needs_sponsorship="Yes"))
    assert decide(choice("Authorized to work without sponsorship?", ["Yes", "No"]), needs).value == "No"


def test_a_missing_profile_answer_blocks_a_required_field_and_skips_an_optional_one():
    blank = Context(profile=ProfileSettings(name="A B", work_authorization="", needs_sponsorship=""))
    q = "Are you legally authorized to work in the United States?"
    assert decide(choice(q, ["Yes", "No"], required=True), blank).action == NEEDS
    assert decide(choice(q, ["Yes", "No"], required=False), blank).action == SKIP


def test_self_identification_declines_by_default_and_never_guesses():
    c = ctx()
    opts = ["Male", "Female", "Non-binary", "I don't wish to answer"]
    d = decide(choice("Gender", opts), c)
    assert (d.action, d.value) == (FILL, "I don't wish to answer")
    # no decline option on a required question: stop, do not pick one
    assert decide(choice("Gender", ["Male", "Female"], required=True), c).action == NEEDS
    assert decide(choice("Gender", ["Male", "Female"], required=False), c).action == SKIP
    # an answer you chose yourself is honoured
    chosen = decide(choice("Race", ["Asian", "White", "Decline to self-identify"]), ctx(facts={"eeo_race": "Asian"}))
    assert chosen.value == "Asian"


def test_legal_and_personal_questions_are_never_handed_to_the_chat():
    c = ctx(chat_enabled=True)
    for label in ("Have you ever been convicted of a felony?", "What is your current salary?",
                  "Please describe your immigration status", "What is your date of birth?"):
        d = decide(text(label, True, kind="textarea"), c)
        assert d.action == NEEDS, label
        assert d.action != CHAT


def test_open_questions_go_to_the_chat_only_when_it_is_available():
    q = text("Describe your experience building RAG systems.", True, kind="textarea", max_chars=1200)
    assert decide(q, ctx(chat_enabled=True)).action == CHAT
    assert decide(q, ctx(chat_enabled=False)).action == NEEDS
    assert decide(text("Why do you want to work here?", False), ctx(chat_enabled=False)).action == SKIP
    skill_years = text("How many years of experience do you have with Kubernetes?", True, kind="number")
    assert decide(skill_years, ctx(chat_enabled=True)).action == CHAT  # about one skill: the resume knows


def test_a_job_fit_choice_goes_to_the_chat_and_stops_when_the_chat_is_off():
    office = choice("Which office would you prefer?", ["Austin", "Remote"])
    assert decide(office, ctx(chat_enabled=True)).action == CHAT
    stopped = decide(office, ctx(chat_enabled=False))
    assert stopped.action == NEEDS and "Not covered" in stopped.reason


def test_consent_boxes_follow_your_setting_and_text_message_consent_defaults_to_no():
    acknowledge = Field(ref="c", kind="checkbox", label="I acknowledge the Privacy Notice", required=True)
    assert decide(acknowledge, ctx()).action == FILL
    assert decide(acknowledge, ctx(facts={"agree_consents": "No"})).action == NEEDS
    sms = Field(ref="s", kind="checkbox", label="I agree to receive text messages (SMS)", required=False)
    assert decide(sms, ctx()).action == SKIP
    assert decide(sms, ctx(facts={"sms_consent": "Yes"})).action == FILL
    marketing = Field(ref="m", kind="checkbox", label="Keep me updated about future opportunities", required=True)
    assert decide(marketing, ctx()).action == SKIP
    mystery = Field(ref="x", kind="checkbox", label="I certify I am not a robot's cousin", required=True)
    assert decide(Field(ref="y", kind="checkbox", label="Subscribe me to Fridays", required=True), ctx()).action == NEEDS
    assert mystery  # (the first clause above shows unknown boxes are not ticked blindly)


def test_files_use_the_generated_documents():
    c = ctx(has_resume=True, has_cover=True)
    assert decide(Field(ref="f", kind="file", label="Resume/CV", slot="resume", required=True), c).value == RESUME_FILE
    assert decide(Field(ref="f", kind="file", label="Cover Letter", slot="cover"), c).value == COVER_FILE
    no_cover = ctx(has_cover=False)
    assert decide(Field(ref="f", kind="file", label="Cover Letter", slot="cover"), no_cover).action == SKIP
    assert decide(Field(ref="f", kind="file", label="Transcript", slot="other", required=True), c).action == NEEDS
    assert decide(Field(ref="f", kind="file", label="Resume", slot="resume", required=True),
                  ctx(has_resume=False)).action == NEEDS


def test_a_cover_letter_text_box_gets_the_generated_letter():
    box = text("Cover Letter", False, kind="textarea")
    assert decide(box, ctx(cover_letter_text="Dear team,")).value == "Dear team,"


def test_option_choice_helpers():
    yn = [Option("Yes"), Option("No")]
    assert choose_option("No", yn).label == "No"
    ranges = [Option("0-2 years"), Option("3-5 years"), Option("6-10 years"), Option("10+ years")]
    assert choose_option("8", ranges, "years_experience").label == "6-10 years"
    assert choose_option("14", ranges, "years_experience").label == "10+ years"
    sources = [Option("Indeed"), Option("Referral"), Option("Other")]
    assert choose_option("LinkedIn", sources, "referral_source").label == "Other"
    assert choose_option("LinkedIn", [Option("LinkedIn"), Option("Other")], "referral_source").label == "LinkedIn"
    assert choose_option("Maybe", yn) is None
    assert choose_option(DECLINE, [Option("Male"), Option("Prefer not to say")]).label == "Prefer not to say"
    assert choose_option(DECLINE, [Option("Male"), Option("Female")]) is None


def test_facts_are_cleaned_on_load():
    cleaned = clean_facts({"relocation": "yes", "over_18": "maybe", "salary_expectation": " $1 ", "bogus": "x", "city": ""})
    assert cleaned == {"relocation": "Yes", "salary_expectation": "$1"}
    assert clean_facts("nonsense") == {}


def test_a_skill_experience_choice_goes_to_the_chat_only_when_it_can_pick_from_the_options():
    options = ["Less than 1 year", "1-3 years", "3-5 years", "5+ years"]
    for kind in ("radio", "select", "checkgroup"):
        q = choice("How many years of experience do you have with Snowflake?", options, kind=kind)
        assert decide(q, ctx(chat_enabled=True)).action == CHAT, kind
        assert decide(q, ctx(chat_enabled=False)).action == NEEDS, kind
    # work the resume can speak to goes to the chat; a personal question never does
    fit = ctx(chat_enabled=True)
    assert decide(choice("Have you shipped LLM-powered or agentic AI systems to production?", ["Yes", "No"]), fit).action == CHAT
    assert decide(choice("Are you more product-focused or systems-focused?", ["Product-Focused", "Systems-Focused"]), fit).action == CHAT
    assert decide(choice("Do you have a disability?", ["Yes", "No"]), fit).action != CHAT
    # no options read from the page (a type-ahead): nothing to choose from
    assert decide(choice("Years of experience with Snowflake", [], kind="combobox"), ctx(chat_enabled=True)).action == NEEDS


def test_a_missing_profile_answer_says_where_to_fill_it_in():
    blank = Context(profile=ProfileSettings(name="A B"))
    d = decide(text("LinkedIn Profile", True), blank)
    assert d.action == NEEDS and "Your answers" in d.reason and "LinkedIn Profile" in d.reason


RESUME = {"experience": [
    {"company": "Oscar Health", "title": "Senior Data Engineer", "dates": "Mar 2022 - Present"},
    {"company": "Acme Analytics", "role_title": "Data Engineer", "dates": "Jan 2016 - Feb 2022"}]}


def test_current_or_previous_employer_and_title_come_from_the_resume():
    c = Context(profile=PROFILE, facts={}, resume=RESUME, chat_enabled=True, today=date(2026, 11, 5))
    employer = decide(text("Who is your current or previous employer?", True), c)
    title = decide(text("What is your current or previous job title?", True), c)
    assert employer.action == FILL and employer.value == "Oscar Health"
    assert title.action == FILL and title.value == "Senior Data Engineer"


def test_a_country_checkbox_uses_the_united_states_option_even_when_the_wording_is_longer():
    c = ctx(facts={"country": "United States"}, chat_enabled=True)
    box = choice(
        "Please select the country or countries you anticipate working in for the role in which you are applying.",
        ["Canada", "United States of America", "Germany"], kind="checkgroup")
    picked = decide(box, c)
    assert picked.action == FILL and picked.value == "United States of America"


def test_employer_title_and_education_come_from_the_resume_and_profile_not_from_typed_facts():
    c = Context(profile=PROFILE, facts={}, resume=RESUME, today=date(2026, 11, 5))
    assert decide(text("Current Employer", True), c).value == "Oscar Health"
    assert decide(text("Current Job Title", True), c).value == "Senior Data Engineer"
    assert decide(text("School", True), c).value == PROFILE.education_school
    assert decide(text("Degree", True), c).value == PROFILE.education_degree
    # a resume without that information is not guessed at
    assert decide(text("Current Employer", True), Context(profile=PROFILE, facts={}, resume={})).action == NEEDS
    assert decide(text("Current Employer", False), Context(profile=PROFILE, facts={}, resume={})).action == SKIP


def test_years_of_experience_is_asked_of_the_chat_and_falls_back_to_the_resume_dates():
    ranges = ["0-2 years", "3-5 years", "6-10 years", "10+ years"]
    c = Context(profile=PROFILE, facts={}, resume=RESUME, chat_enabled=True, today=date(2026, 11, 5))
    assert decide(text("How many years of professional experience do you have?", True, kind="number"), c).action == CHAT
    assert decide(choice("Years of experience", ranges), c).action == CHAT
    plain = Context(profile=PROFILE, facts={}, resume=RESUME, chat_enabled=False, today=date(2026, 11, 5))
    assert decide(text("How many years of professional experience do you have?", True, kind="number"), plain).value == "10"
    assert decide(choice("How many years of experience do you have?", ranges), plain).value == "6-10 years"  # ten years falls in 6-10


def test_the_facts_list_no_longer_asks_for_what_the_resume_already_says():
    from core.apply_facts import FACT_KEYS

    assert not FACT_KEYS & {"current_company", "current_title", "years_experience"}


GITLAB_LABELS = [
    # a question that merely mentions a place or an employer is not the address / employer field
    ("This role is open to candidates in the United States. Where are you based?", ["Yes", "No"]),
    ("Are you subject to any employment agreements or restrictions with your current employer?", ["Yes", "No"]),
    ("Will you now or in the future require sponsorship in the city and state where you work?", ["Yes", "No"]),
]


@pytest.mark.parametrize("label, options", GITLAB_LABELS)
def test_a_long_or_yes_no_question_is_never_answered_with_an_address_or_an_employer(label, options):
    assert classify(label) not in {"location", "city", "state", "country", "current_company", "school"}
    d = decide(choice(label, options, kind="combobox"), Context(profile=PROFILE, facts={"city": "Austin", "state": "TX"},
                                                                resume=RESUME))
    assert d.value not in {"Austin, TX", "Oscar Health", "Austin"}


def test_a_dropdown_whose_choices_could_not_be_read_is_never_typed_into_unless_it_is_a_lookup():
    blank_dropdown = Field(ref="r", kind="combobox", label="Will you now or in the future require sponsorship?", required=True)
    d = decide(blank_dropdown, ctx())
    assert d.action == FILL and d.value in ("No", "Yes") or d.action == NEEDS
    unknown = Field(ref="r", kind="combobox", label="Preferred pronouns at work", required=True)
    assert decide(unknown, ctx(facts={"pronouns": "they/them"})).action == NEEDS  # list not readable: not typed
    lookup = Field(ref="r", kind="combobox", label="Location (City)", required=True)
    assert decide(lookup, ctx()).value == "Austin, TX"  # a city lookup is typed and a suggestion picked
    country = Field(ref="r", kind="combobox", label="Country", required=True,
                    options=(Option("Canada"), Option("Mexico")))
    picked = decide(country, ctx(facts={"country": "United States"}))
    assert picked.action == FILL and picked.value == "United States"  # the visible slice missed it: type to filter


def test_greenhouse_questions_from_a_real_form_are_answered_from_your_facts():
    states = ["Alabama", "Florida", "Other"]
    state = decide(choice("State (If you do not live in any of the states listed, please select Other.)", states),
                   ctx(facts={"state": "Florida"}))
    assert state.value == "Florida"
    assert decide(choice("State (If you do not live in any of the states listed, please select Other.)", states),
                  ctx(facts={"state": "FL"})).value == "Florida"
    perform = decide(choice("Can you perform the essential functions of the job with or without reasonable accommodation?",
                            ["Yes", "No"]), ctx(chat_enabled=True))
    assert perform.value == "Yes" and perform.action != CHAT
    prior = decide(choice("Have you worked for BambooHR previously?", ["Yes", "No"]),
                   ctx(chat_enabled=True, facts={"previously_employed": "No"}))
    assert prior.value == "No" and prior.action != CHAT
    follow = text("If Yes, please enter your legal name while working at BambooHR previously. If No, please enter N/A.",
                  True)
    assert decide(follow, ctx(chat_enabled=True)).value == "N/A"
    petition = "Will you now or in the future require BambooHR to file a petition or application for employment - based status on your behalf?"
    assert decide(choice(petition, ["Yes", "No"]), ctx()).value == "No"
    process = ("Will you require Quisitive to file, sign, complete, certify, support or participate in any "
               "immigration or work authorization process now or in the future?")
    assert decide(choice(process, ["Yes", "No"]), ctx()).value == "No"  # sponsorship, not "yes I am authorized"
    veteran = ["I don't wish to answer", "I identify as one or more of the classifications of a protected veteran",
               "I am not a protected veteran"]
    assert decide(choice("Veteran Status", veteran), ctx(facts={"eeo_veteran": "No"})).value == "I am not a protected veteran"
    consent = decide(choice("By submitting your application, you consent to processing and agree to the Privacy Notice.",
                            ["I agree"]), ctx(facts={"agree_consents": "Yes"}))
    assert consent.value == "I agree"
    refused = decide(choice("By submitting your application, you consent to processing and agree to the Privacy Notice.",
                            ["I agree"], ), ctx(facts={"agree_consents": "No"}))
    assert refused.action == NEEDS and "set to No" in refused.reason
    group = choice("Do any of the following apply to you? (Check all that apply)",
                   ["I have a family member who is currently employed by BambooHR.", "None of the above apply to me."],
                   kind="checkgroup")
    assert decide(group, ctx()).value == "None of the above apply to me."
    assert decide(choice("Current State", ["Alabama", "Florida"]), ctx(facts={"state": "Florida"})).value == "Florida"
    assert decide(choice("Current Country", ["Germany", "United States"]), ctx(facts={"country": "United States"})).value == "United States"
    texted = decide(choice("Can NinjaOne text you updates about your application?",
                           ["Yes, NinjaOne can text me updates about my application.",
                            "No, NinjaOne cannot text me updates about my application."]),
                    ctx(facts={"sms_consent": "Yes"}))
    assert texted.value.startswith("Yes")
    setup = decide(choice(
        "Can you work in the location and workplace type (remote, hybrid, or onsite) listed in the job posting? "
        "If the role is fully remote and you can work from where you live right now, select “Yes, I can work this way now.\"",
        ["Yes, I can work this way now.", "Yes, I am willing to relocate at my own expense to work this way.",
         "No, I cannot work this way.", "No, I only want to work remotely."]),
        ctx(facts={"onsite_ok": "No"}))
    assert setup.value == "Yes, I can work this way now."


def test_an_optional_follow_up_to_another_answer_is_left_alone():
    follow_up = Field(ref="r", kind="text", required=False,
                      label='If you selected "REST OF WORLD" in the previous question, please let us know where you are based below:')
    assert decide(follow_up, ctx(chat_enabled=True)).action == SKIP


def test_point_does_not_treat_relative_to_as_a_family_question_and_answers_state_and_status():
    title = "In considering this role, how important is the title/level to you, relative to scope, impact, and growth opportunity?"
    assert classify(title) != "has_relatives"
    assert decide(choice(title, ["Title/Level Is Important", "Scope Is Important", "N/A"]), ctx()).action == NEEDS
    assert decide(choice(title, ["Title/Level Is Important", "Scope Is Important", "N/A"]), ctx(chat_enabled=True)).action == CHAT
    assert classify("Do you have relatives who work here?") == "has_relatives"
    state = decide(choice("What state are you currently located in?", ["Alabama", "Florida", "Texas"]),
                   ctx(facts={"state": "Florida"}))
    assert state.value == "Florida"
    status = [
        "I am authorized to work in the U.S. and will not require sponsorship now or in the future",
        "I currently hold an H-1B visa that can be transferred",
        "I require employer-sponsored work authorization now or in the future",
    ]
    picked = decide(choice("Which of the following best describes your current U.S. work authorization status?", status), ctx())
    assert picked.value.startswith("I am authorized to work")
    sponsored = Context(profile=ProfileSettings(name="A B", work_authorization="Yes", needs_sponsorship="Yes"))
    assert decide(choice("Which of the following best describes your current U.S. work authorization status?", status),
                  sponsored).value.startswith("I require")


def test_eso_history_questions_are_the_previously_employed_fact():
    c = ctx(chat_enabled=True, facts={"previously_employed": "No"})
    applied = decide(choice("Have you applied with ESO in the past?", ["Yes", "No"]), c)
    assert applied.value == "No" and applied.action != CHAT
    worked = decide(choice("Have you worked with ESO or an ESO acquired company in the past?", ["Yes", "No"]), c)
    assert worked.value == "No" and worked.action != CHAT


def test_a_dial_code_suffix_still_counts_as_the_country_being_selected():
    from core.form_filler import _choice_kept

    assert _choice_kept("United States +1", "United States")
    assert not _choice_kept("United States +1", "")
    assert not _choice_kept("United States +1", "Select...")


def test_stripe_place_questions_and_an_empty_degree_list_come_from_your_facts():
    facts = {"country": "United States", "city": "Mount Dora", "state": "Florida"}
    c = ctx(facts=facts, chat_enabled=True)
    reside = decide(choice("Please select the country where you currently reside.", ["Canada", "US", "Germany"]), c)
    assert reside.value == "US" and reside.action != CHAT
    work = decide(choice(
        "Please select the country or countries you anticipate working in for the role in which you are applying.",
        ["Canada", "United States"]), c)
    assert work.value == "United States"
    city = text("If located in the US, in what city and state do you reside?", True)
    assert decide(city, c).value == "Mount Dora, Florida" and decide(city, c).action != CHAT
    degree = Field(ref="r", kind="combobox", label="Degree", required=True)
    picked = decide(degree, c)
    assert picked.action == FILL and picked.value == "Bachelor"
    listed = decide(choice("Degree", ["High School", "Bachelor's Degree", "Master's Degree", "Doctorate"]), c)
    assert listed.action == FILL and listed.value == "Bachelor's Degree"


def test_saved_wordings_match_the_sentence_a_greenhouse_form_uses():
    from core.apply_facts import clean_facts

    sony_veteran = [
        "I identify as one or more of the above categories of \u201cprotected veteran\u201d",
        "I do not fall into one of the above categories of \u201cprotected veteran\u201d",
        "I previously served in the military (US), but none of the above apply to me.",
        "I don't wish to answer",
    ]
    picked = decide(choice("Are you a protected veteran?", sony_veteran), ctx(facts={"eeo_veteran": "No"}))
    assert "do not fall into" in picked.value
    phrase = decide(choice("Are you a protected veteran?", sony_veteran),
                    ctx(facts={"eeo_veteran": "I am not a protected veteran"}))
    assert phrase.value == picked.value
    race = decide(choice("Please identify your race/ethnicity",
                         ["Caucasian", "Asian (Not Hispanic or Latino)", "White"]),
                  ctx(facts={"eeo_race": "Asian"}))
    assert race.value == "Asian (Not Hispanic or Latino)"
    orientation = decide(choice("Sexual Orientation", ["Asexual", "Gay/Lesbian", "Heterosexual"]),
                         ctx(facts={"eeo_orientation": "Heterosexual / Straight"}))
    assert orientation.value == "Heterosexual"
    located = decide(choice("Are you currently located in the United States?", ["Yes", "No"]),
                     ctx(facts={"country": "United States"}))
    assert located.value == "Yes"
    unset = decide(choice("Do you have any agreement with a current or past employer that limits where you can work?",
                          ["Yes, I have an agreement that limits where I can work.",
                           "No, I do not have an agreement that limits where I can work."]), ctx())
    assert unset.action == NEEDS
    chosen = decide(choice("Do you have any agreement with a current or past employer that limits where you can work?",
                           ["Yes, I have an agreement that limits where I can work.",
                            "No, I do not have an agreement that limits where I can work."]),
                    ctx(facts={"non_compete": "No"}))
    assert chosen.value.startswith("No")
    certify = decide(choice("By selecting \"Yes\", I am certifying that the information I have provided is true.",
                            ["Yes", "No"]), ctx(facts={"agree_consents": "No"}))
    assert certify.action == NEEDS and "set to No" in certify.reason
    cleaned = clean_facts({"eeo_race": "Assian", "eeo_orientation": "Straight", "eeo_veteran": "No", "eeo_gender": "Male"})
    assert cleaned["eeo_race"] == "Asian"
    assert cleaned["eeo_orientation"] == "Heterosexual / Straight"
    assert cleaned["eeo_veteran"] == "I am not a protected veteran"
    assert cleaned["eeo_gender"] == "Male"


def test_a_stopped_select_question_offers_its_choices_and_a_file_error_does_not():
    from core.apply_facts import questions_from_attempt

    race = questions_from_attempt(
        {"reason": "Still empty after filling: Please identify your race", "plan_json": "[]"},
        [], {"eeo_race": "Asian"})
    assert len(race) == 1 and race[0]["locked"] is False
    assert race[0]["label"] == "Please identify your race"
    assert "Asian" in race[0]["options"] and race[0]["answer"] == "Asian"
    assert questions_from_attempt(
        {"reason": "The site rejected the form: Resume/CV is required.", "plan_json": "[]"}, []) == []
    degree = questions_from_attempt(
        {"reason": "The chat could not answer 'Degree': ChatGPT left Send disabled.", "plan_json": "[]"}, [])
    assert degree[0]["label"] == "Degree" and "Bachelor's Degree" in degree[0]["options"]
    bot = questions_from_attempt(
        {"reason": "This asks whether the submission is automated, so it is left blank", "plan_json": "[]"}, [])
    assert bot[0]["locked"] is True and bot[0]["options"] == []


def test_an_answer_you_type_is_used_on_the_next_try_and_a_bot_check_stays_blank():
    from core.apply_facts import answered_form_questions, norm_question, store_manual_answers

    label = "Describe a product you shipped"
    saved = store_manual_answers([], [(label, "I shipped the billing agent.", [])])
    typed = decide(text(label, kind="textarea"), ctx(chat_enabled=True, form_answers=answered_form_questions(saved)))
    assert typed.action == FILL and typed.value == "I shipped the billing agent."
    office = "Which office would you prefer?"
    saved = store_manual_answers([], [(office, "Remote", ["Austin", "Remote"])])
    picked = decide(choice(office, ["Austin", "Remote"]), ctx(chat_enabled=True, form_answers=answered_form_questions(saved)))
    assert picked.action == FILL and picked.value == "Remote"
    bot = choice("Are you human?", ["I am a human being", "I am an AI or automated program"])
    left = decide(bot, ctx(form_answers={norm_question(bot.label): "I am a human being"}))
    assert left.action == NEEDS and left.value == "" and "automated" in left.reason


def test_a_question_you_already_answered_is_reused_and_a_new_one_is_remembered():
    from core.apply_facts import answered_form_questions, merge_form_questions
    from core.greenhouse import GreenhouseApplier

    label = "Which office would you prefer?"
    bank = answered_form_questions([{"label": label, "options": ["Austin", "Remote"], "answer": "Remote"}])
    reused = decide(choice(label, ["Austin", "Remote"]), ctx(chat_enabled=True, form_answers=bank))
    assert reused.value == "Remote" and reused.action == FILL
    seen = []
    applier = GreenhouseApplier()
    applier.remember = lambda text, options: seen.append((text, options))
    _plan, needs, _todo = applier._plan([choice(label, ["Austin", "Remote"])], ctx())
    assert needs and seen == [(label, ["Austin", "Remote"])]
    merged = merge_form_questions([], seen)
    assert merged[0]["label"] == label and merged[0]["answer"] == ""
    again = merge_form_questions([{"label": label, "options": ["Austin", "Remote"], "answer": "Remote"}], seen)
    assert again[0]["answer"] == "Remote"


def test_have_you_questions_about_experience_are_answered_by_the_chat_from_the_resume():
    q = choice("Have you been part of an on-call rotation and personally involved in responding to production incidents?",
               ["Yes", "No"], kind="combobox")
    assert decide(q, ctx(chat_enabled=True)).action == CHAT
    assert decide(q, ctx(chat_enabled=False)).action == NEEDS
    # but not the ones about the law or the company: those have rules or are held
    assert decide(choice("Have you ever been convicted of a crime?", ["Yes", "No"]), ctx(chat_enabled=True)).action == NEEDS
    assert decide(choice("Have you previously worked for this company?", ["Yes", "No"]), ctx(chat_enabled=True, facts={"previously_employed": "No"})).value == "No"


def test_where_you_live_answers_the_time_zone_and_a_bot_check_is_left_blank():
    east = ctx(facts={"state": "Florida"}, chat_enabled=True)
    assert decide(choice("Are you EST based?", ["Yes", "No"]), east).value == "Yes"
    assert decide(choice("Are you CST based?", ["Yes", "No"]), east).value == "No"
    assert decide(choice("Are you EST based?", ["Yes", "No"]), ctx(chat_enabled=True)).value == "No"  # Austin is Central
    florida = Context(profile=ProfileSettings(name="A B", location="Mount Dora, Florida, United States"), chat_enabled=True)
    assert decide(choice("Are you EST based?", ["Yes", "No"]), florida).value == "Yes"
    gender = decide(choice("What gender identity do you most closely identify with?",
                           ["Agender", "Man", "Woman", "I don't wish to answer"]),
                    ctx(facts={"eeo_gender": "Male"}))
    assert gender.value == "Man"
    trans = decide(choice("Are you a person of transgender experience?",
                          ["Yes", "No", "I don't wish to answer"]), ctx(chat_enabled=True))
    assert trans.action == FILL and trans.value == "I don't wish to answer"
    assert decide(choice("Are you a person of transgender experience?", ["Yes", "No"], required=True),
                  ctx(chat_enabled=True)).action == NEEDS
    bot = decide(choice("Which of the following best describes you?",
                        ["I am an AI or automated program", "I am a human being"]), ctx(chat_enabled=True))
    assert bot.action == NEEDS and bot.value == "" and "automated" in bot.reason


def test_the_name_asked_for_in_a_sentence_comes_from_the_profile_not_the_chat():
    from core.form_answers import classify
    assert classify("Please provide your legal first and last name.") == "full_name"
    assert classify("What is your full name?") == "full_name"
    assert classify("Do you have a preferred name different from your legal name?") != "full_name"
    # "preferred name" as a plain field is the first name; it is never worth a question to the chat
    assert classify("What is your preferred name?") == "first_name"
    assert classify("Preferred name") == "first_name"
    assert classify("Do you have a preferred name different from your legal name?") != "first_name"


KOMODO = ("We'd love to learn more about your working preferences. Roles at Komodo are labeled as either Remote, "
          "Location Flexible (within a specific region or commutable to any U.S. office) or Location Specific.")
ARRANGEMENTS = ["Fully remote (United States)", "Hybrid (New York City)", "Full-time in office (Chicago)",
                "Decline to answer"]


def test_a_list_of_working_arrangements_gets_the_remote_choice_when_remote_is_fine():
    d = decide(choice(KOMODO, ARRANGEMENTS), ctx(facts={"remote_ok": "Yes", "onsite_ok": "Yes"}))
    assert (d.action, d.value) == (FILL, "Fully remote (United States)")


def test_a_list_of_working_arrangements_is_left_to_you_when_remote_is_not_clearly_the_answer():
    assert decide(choice(KOMODO, ARRANGEMENTS), ctx(facts={"remote_ok": "No", "onsite_ok": "Yes"})).action == NEEDS
    two = ["Remote (US)", "Remote (Canada)", "Hybrid"]
    assert decide(choice(KOMODO, two), ctx(facts={"remote_ok": "Yes", "onsite_ok": "Yes"})).action == NEEDS


def test_proof_of_identity_and_work_authorization_is_answered_from_your_facts_never_by_the_chat():
    from core.form_answers import classify
    q = ("If hired, can you produce acceptable evidence of your identity and authorization to work in the country "
         "where this position is located?")
    assert classify(q) == "work_authorization"
    d = decide(choice(q, ["Yes", "No"]), ctx(chat_enabled=True))
    assert (d.action, d.value, d.source) == (FILL, "Yes", "profile") or (d.action, d.value) == (FILL, "Yes")


def test_a_demographic_list_worded_differently_gets_the_decline_choice_not_a_guess():
    labels = ["East Asian", "South Asian", "White", "I don't wish to answer"]
    d = decide(choice("Please select up to 2 ethnicities that you most closely identify with.", labels),
               ctx(facts={"eeo_race": "Asian"}))
    assert (d.action, d.value) == (FILL, "I don't wish to answer")
    plain = decide(choice("Please identify your race", ["Asian", "White", "Decline To Self Identify"]),
                   ctx(facts={"eeo_race": "Asian"}))
    assert (plain.action, plain.value) == (FILL, "Asian")


def test_sponsorship_choices_written_as_statements_are_matched_to_your_yes_or_no():
    q = "Will you now or in the future require visa sponsorship?"
    labels = ["Requires sponsorship for employment authorization", "Does not require sponsorship"]
    d = decide(choice(q, labels), ctx())
    assert (d.action, d.value) == (FILL, "Does not require sponsorship")
    three = labels + ["Other"]
    assert decide(choice(q, three), ctx()).action == NEEDS


def test_the_age_question_is_recognised_in_its_common_wordings():
    from core.form_answers import classify
    for q in ("Are you over the age of 18 ?", "Are you at least 18 years old?", "Are you 18 years of age or older?",
              "Are you 18 or older?"):
        assert classify(q) == "over_18", q


def test_an_acknowledgement_with_one_choice_worded_its_own_way_is_given():
    q = ("By submitting this application, I acknowledge that I have read and understand Fivetran's Applicant "
         "Privacy Notice regarding the collection, use, and sharing of my personal information.")
    d = decide(choice(q, ["Acknowledge/Confirm"]), ctx())
    assert (d.action, d.value) == (FILL, "Acknowledge/Confirm")
    two = decide(choice(q, ["I do not agree", "I agree"]), ctx())
    assert (two.action, two.value) == (FILL, "I agree")


WEKA = ("Are you legally authorized to work in the country where this position is located? If yes, please specify "
        "your work authorization type (e.g., citizenship, permanent residency, work visa, etc.).")


def test_authorized_and_which_status_in_one_text_question_is_answered_from_both_saved_answers():
    d = decide(text(WEKA, required=True), ctx(facts={"visa_status": "US citizen"}))
    assert (d.action, d.value) == (FILL, "Yes, US citizen")
    assert decide(text(WEKA, required=True), ctx()).action == NEEDS  # no saved work status: still yours to answer


def test_a_question_the_form_does_not_require_is_skipped_when_blanks_are_allowed():
    from core.form_answers import OPTIONAL_REASON

    profile = ProfileSettings(name="Mary Ann Berg", email="mary@example.com", linkedin_url="https://linkedin.com/in/mary")
    optional = Field(ref="a", kind="text", label="LinkedIn Profile", required=False)
    required = Field(ref="b", kind="text", label="Email", required=True)
    cover = Field(ref="c", kind="file", label="Cover Letter", required=False, slot="cover")
    keep = Context(profile=profile, has_cover=True, skip_optional=False)
    skip = Context(profile=profile, has_cover=True, skip_optional=True)
    assert decide(optional, keep).action == FILL  # the old behaviour is still there when switched off
    assert (decide(optional, skip).action, decide(optional, skip).reason) == (SKIP, OPTIONAL_REASON)
    assert decide(required, skip).action == FILL and decide(required, skip).value == "mary@example.com"
    assert decide(cover, skip).action == FILL  # attachments are not questions: the cover letter is still sent
    chat_question = Field(ref="d", kind="textarea", label="Why do you want to work here?", required=False)
    assert decide(chat_question, Context(profile=profile, chat_enabled=True, skip_optional=True)).action == SKIP


TRIPLE_WHALE = ("By submitting your application, you consent to Triple Whale collecting and using your personal data for "
                "purposes related to recruitment, organizational planning on a global scale, and other relevant activities.")


def test_a_consent_notice_is_not_mistaken_for_a_question_about_relatives():
    assert classify(TRIPLE_WHALE) == "agree_consents"
    assert classify("Are you related to anyone who works at Acme?") == "has_relatives"


def test_a_consent_list_with_one_acceptance_option_gets_that_option():
    field = choice(TRIPLE_WHALE, ["Accept"], kind="combobox")
    d = decide(field, ctx())
    assert (d.action, d.value) == (FILL, "Accept")


def test_the_single_option_is_still_taken_if_the_question_is_misread():
    odd = choice("You consent to us keeping your details", ["Accept"], kind="combobox")
    assert decide(odd, ctx(facts={"has_relatives": "No", "agree_consents": "No"})).action == NEEDS  # "No" is never forced to "Accept"
    from core.form_answers import _only_acknowledgement
    assert _only_acknowledgement(odd).label == "Accept"
    assert _only_acknowledgement(choice("Which office?", ["Accept"])) is None


def _chat_ctx(**kw):
    return ctx(chat_enabled=True, **kw)


def test_an_answer_that_matches_no_option_goes_to_the_chat_instead_of_stopping():
    sponsorship = choice("Do you require sponsorship?", ["I will not require sponsorship", "Yes - OPT", "Other sponsorship required"])
    assert decide(sponsorship, ctx()).action == NEEDS  # no chat: nothing to ask
    assert decide(sponsorship, _chat_ctx()).action == CHAT
    # a voluntary demographic list never goes to the chat
    assert decide(choice("Gender", ["Man", "Woman"]), _chat_ctx()).action != CHAT


def test_a_question_about_two_things_goes_to_the_chat_with_its_options():
    two = choice("Are you eligible to work in the United States and do you need sponsorship now or later?", ["Yes", "No"])
    assert decide(two, ctx()).action == NEEDS
    assert decide(two, _chat_ctx()).action == CHAT


def test_citizenship_is_asked_to_the_chat_which_is_given_your_facts():
    citizen = choice("Are you currently a U.S. citizen?", ["Yes", "No"])
    assert decide(citizen, ctx()).action == NEEDS
    assert decide(citizen, _chat_ctx()).action == CHAT
    # other sensitive questions are still never handed over
    assert decide(choice("Have you ever been convicted of a felony?", ["Yes", "No"]), _chat_ctx()).action == NEEDS


def test_authorization_in_the_country_where_you_live_is_not_the_country_field():
    label = "Your authorization to work in the country where you live:"
    assert classify(label) == "work_authorization"
    assert classify("Country") == "country"


def test_a_full_state_name_picks_the_abbreviation_in_the_list():
    state = choice("State", ["AL", "FL", "GA"], kind="combobox")
    d = decide(state, ctx(facts={"state": "Florida"}))
    assert (d.action, d.value) == (FILL, "FL")


def test_work_history_dates_come_from_the_resume_and_a_number_box_never_gets_words():
    resume = {"experience": [{"company": "Acme", "title": "Engineer", "dates": "Jun 2023 - Sep 2026"}]}
    year = Field(ref="y", kind="number", label="Start date year", required=True, input_type="number")
    d = decide(year, ctx(resume=resume))
    assert (d.action, d.value) == (FILL, "2023")
    assert decide(Field(ref="y", kind="number", label="End date year", required=True, input_type="number"),
                  ctx(resume=resume)).value == "2026"
    month = Field(ref="m", kind="number", label="Start date month", required=True, input_type="number")
    assert decide(month, ctx(resume=resume)).value == "6"
    # a start date that is really "when can you start" must not put words into a number box
    no_dates = decide(Field(ref="y", kind="number", label="Start date year", required=True, input_type="number"), ctx())
    assert no_dates.action == NEEDS
    words = Field(ref="n", kind="number", label="Notice period", required=True, input_type="number")
    assert decide(words, ctx(facts={"notice_period": "2 weeks"})).action == NEEDS


def test_your_facts_are_written_for_the_chat():
    from core.personal_facts import personal_facts_text

    text = personal_facts_text(PROFILE, {"visa_status": "US citizen", "salary_expectation": "$150,000", "eeo_gender": "Decline"},
                               {"are you eligible to work": "Yes"})
    assert "US citizen" in text and "Austin, TX" in text and "Legally authorized" in text
    assert "150,000" not in text  # the salary has its own rules
    assert "are you eligible to work" in text
    assert "Gender" not in text and "prefers not to say" not in text  # demographic answers never go to the chat


def test_an_earliest_start_date_question_is_not_a_work_history_date():
    assert classify("When is your earliest start date? (month/year)") == "notice_period"
    assert classify("Start Date - Year*") == "history_start_year"


def test_addresses_names_gpa_and_certifications_come_from_your_details_not_the_chat():
    facts = {"address": "1 Main St", "city": "Austin", "state": "TX", "zip": "78701"}
    for label in ("Please provide your full home address.", "Current Address", "Permanent Legal Address"):
        d = decide(text(label, required=True), ctx(facts=facts, chat_enabled=True))
        assert (d.action, d.value) == (FILL, "1 Main St, Austin, TX 78701"), label
    assert decide(text("Street Address", required=True), ctx(facts=facts)).value == "1 Main St"
    assert decide(text("Preferred Last Name", required=True), ctx()).value == "van der Berg"
    gpa = text("What is your cumulative GPA?", required=True)
    assert decide(gpa, ctx(chat_enabled=True)).action == NEEDS  # not saved: never written by the chat
    assert decide(gpa, ctx(facts={"gpa": "3.6"})).value == "3.6"
    certs = text("Please share any relevant certifications you have received.", required=True)
    assert decide(certs, ctx(facts={"certifications": "None"}, chat_enabled=True)).value == "None"


def test_the_facts_for_the_chat_are_short_and_say_what_answers_questions():
    from core.personal_facts import personal_facts_text

    facts = {"address": "1 Main St", "city": "Austin", "state": "TX", "zip": "78701", "country": "United States",
             "certifications": "None", "other_names": "None", "relocation": "Yes", "notice_period": "Immediately",
             "about_me": "I can travel.", "sms_consent": "Yes", "pronouns": "She/Her", "eeo_race": "Asian"}
    many = {f"question {n}": "Yes" for n in range(40)}
    text = personal_facts_text(PROFILE, facts, many)
    assert "1 Main St, Austin, TX 78701, United States" in text and "my only address" in text
    assert "Certifications: None" in text and "Other names or aliases: None" in text and "I can travel." in text
    assert "She/Her" not in text and "Asian" not in text and "text messages" not in text
    assert "question 39" in text and "question 27" not in text  # only the latest saved answers
    assert len(text) < 2500


def test_aliases_and_other_names_come_from_your_answers():
    assert decide(text("List any aliases or other name(s) you have used", required=True),
                  ctx(facts={"other_names": "None"})).value == "None"


def test_a_question_that_asks_for_an_answer_without_ai_is_never_given_to_the_chat():
    from core.form_answers import NO_AI_REASON

    why = text("Why are you interested in this role? (While we love AI please do not use AI/ ChatGPT for any part)",
               required=True, kind="textarea")
    d = decide(why, ctx(chat_enabled=True))
    assert d.action == NEEDS and d.reason.startswith(NO_AI_REASON)
    personal = text("In your own words, what draws you to us? Please answer personally (without using AI).",
                    required=True, kind="textarea")
    assert decide(personal, ctx(chat_enabled=True)).reason.startswith(NO_AI_REASON)
    assert decide(text("How do you use AI tools day to day?", required=True, kind="textarea"),
                  ctx(chat_enabled=True)).action == CHAT


def test_small_gaps_seen_in_real_forms():
    facts = {"address": "1 Main St", "city": "Austin", "state": "TX", "zip": "78701"}
    assert decide(text("Physical Address", required=True), ctx(facts=facts)).value == "1 Main St, Austin, TX 78701"
    heard = choice("How did you first hear about us?", ["LinkedIn: (job post)", "Other Job Boards: (Indeed, portals)", "Other"],
                   kind="combobox")
    assert decide(heard, ctx(facts={"referral_source": "Indeed"})).value == "Other Job Boards: (Indeed, portals)"
    zones = choice("What is your time zone?", ["UTC-6: Central Time (US), Mexico", "UTC-5: Eastern Time (US), Colombia",
                                               "UTC+2: Eastern Europe, Egypt"], kind="combobox")
    assert decide(zones, ctx(facts={"state": "FL"})).value == "UTC-5: Eastern Time (US), Colombia"
    odd = choice("What is your time zone?", ["GMT-5", "GMT+1"], kind="combobox")
    assert decide(odd, ctx(facts={"state": "FL"}, chat_enabled=True)).action == CHAT
    notice = choice("It is unlawful in Massachusetts to require or administer a lie detector test as a condition of "
                    "employment. An employer who violates this law shall be subject to criminal penalties.",
                    ["I acknowledge I received this notice", "I do not reside in Massachusetts"], kind="combobox")
    assert decide(notice, ctx()).value == "I acknowledge I received this notice"
    context = text("If applicable, please provide additional context to the previous question.", required=True,
                   kind="textarea")
    assert (decide(context, ctx(chat_enabled=True)).action, decide(context, ctx()).value) == (FILL, "N/A")
