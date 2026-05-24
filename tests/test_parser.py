from src.parser import (
    _clean_date_text,
     _normalize_date,
    MalthouseParser,
)

def test_clean_date_text_removes_ui_noise():
    raw = (
        "Dates & Times Sunday 24th May 2026 "
        "Price: £25.50 BOOK NOW"
    )

    cleaned = _clean_date_text(raw)

    assert cleaned == "Sunday 24th May 2026"

def test_normalize_date_returns_iso_format():
    result = _normalize_date(
        "Sunday 24th May 2026",
        "2026"
    )

    assert result == "2026-05-24"


def test_parse_show_detail_extracts_dates_and_performances():
    html = """
    <html>
        <head>
            <title>Hamlet – Malthouse Theatre</title>
        </head>
        <body>
            <h2 class="elementor-heading-title">Hamlet</h2>

            <div>
                Dates & Times Sunday 24th May 2026 7PM BOOK NOW
            </div>

            <div>
                A tragic drama production.
            </div>
        </body>
    </html>
    """

    result = MalthouseParser.parse_show_detail(
        html,
        "https://malthousetheatre.co.uk/event/hamlet/"
    )

    assert result["title"] == "Hamlet"

    assert result["open_date"] == "2026-05-24"
    assert result["close_date"] == "2026-05-24"

    assert result["upcoming_performances"] == [
        {
            "date": "2026-05-24",
            "time": "19:00"
        }
    ]

    assert result["seat_pricing"] == {
        "2026-05-24 19:00": []
    }