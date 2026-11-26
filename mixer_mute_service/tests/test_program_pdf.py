"""Gottesdienstablauf PDF import (app/program_pdf.py).

Verifies: the text fields of a filled form are read (nested names like
"Begrüßung1.Stunde" too), songs and persons come out in the order of the
service, the persons land in the template rows with matching titles (order
kept, a missing row does not shift the rest, song slots left out), a bare
hymn book number finds its song, the import is kept across restarts, and
non-PDFs, foreign forms and bad persons are refused.
"""

import io
import os
import pathlib
import tempfile

_tmp = pathlib.Path(tempfile.mkdtemp(prefix="mixer_program_pdf_"))
(_tmp / "config.yaml").write_text("mixer:\n  ip: 127.0.0.1\n  port: 9\n  max_channels: 8\n")
os.environ["MIXER_CONFIG"] = str(_tmp / "config.yaml")

from fastapi.testclient import TestClient  # noqa: E402
from pypdf import PdfWriter  # noqa: E402
from pypdf.generic import ArrayObject, DictionaryObject, NameObject, TextStringObject  # noqa: E402

from app.main import app  # noqa: E402
from app.program_pdf import assign_persons, parse_ablauf, read_form_fields  # noqa: E402
from app.program_templates import match_lines  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"  {'ok  ' if cond else 'FAIL'} {name}")


def make_pdf(fields):
    """A one-page PDF form; "a.b" names become a parent "a" with a kid "b"."""
    writer = PdfWriter()
    writer.add_blank_page(200, 200)
    top = ArrayObject()
    for name, value in fields.items():
        field = DictionaryObject({NameObject("/FT"): NameObject("/Tx")})
        if value is not None:
            field[NameObject("/V")] = TextStringObject(value)
        if "." in name:
            parent_name, kid_name = name.split(".", 1)
            field[NameObject("/T")] = TextStringObject(kid_name)
            parent = DictionaryObject({NameObject("/T"): TextStringObject(parent_name)})
            parent_ref = writer._add_object(parent)
            field[NameObject("/Parent")] = parent_ref
            parent[NameObject("/Kids")] = ArrayObject([writer._add_object(field)])
            top.append(parent_ref)
        else:
            field[NameObject("/T")] = TextStringObject(name)
            top.append(writer._add_object(field))
    writer._root_object[NameObject("/AcroForm")] = writer._add_object(
        DictionaryObject({NameObject("/Fields"): top}))
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


FORM = {
    "Begrüßung1.Stunde": "Anna Leiterin",
    "Textlesung": "Viktor Leser",
    "Lied1": "101",
    "GebetKnie": "Anna Leiterin",
    "Missionsbericht": "Dora Mission",
    "Lied2": "102",
    "Bibelgespräch1": "Klaus Eins",
    "Bibelgespräch2": "Mia Zwei",
    "BibelgesprächJugend": "Jan Jung",
    "KinderSabbatschule": None,
    "Lied3": "Dir sei Ehre",
    "Bekannmachungen": "Anna Leiterin",
    "Predigt": "J. Prediger",
    "Musikbeitrag2": None,
    "Segenswort": "J. Prediger",
}


def parsing():
    print("== reading the form ==")
    fields = read_form_fields(make_pdf(FORM))
    check("nested and umlaut names normalized", fields.get("begruessung1stunde") == "Anna Leiterin"
          and fields.get("bibelgespraechjugend") == "Jan Jung")
    check("empty field read as empty", fields.get("musikbeitrag2") == "")
    ablauf = parse_ablauf(fields)
    check("songs in order", ablauf["songs"] == ["101", "102", "Dir sei Ehre"])
    points = {p["id"]: p for p in ablauf["points"]}
    check("persons by point", points["begruessung1"]["person"] == "Anna Leiterin"
          and points["predigt"]["person"] == "J. Prediger"
          and points["bekanntmachungen"]["person"] == "Anna Leiterin")
    check("several leaders of the Bibelgespräch joined",
          points["bibelgespraech"]["person"] == "Klaus Eins, Mia Zwei, Jan Jung (Jugend)")
    check("unfilled points kept empty", points["musik2"]["person"] == "" and points["klaviermusik"]["person"] == "")
    for name, data in [("not a PDF", b"hello"), ("foreign form", make_pdf({"Name": "x"})),
                       ("broken PDF", b"%PDF-1.7\n garbage")]:
        try:
            parse_ablauf(read_form_fields(data))
            check(f"{name} refused", False)
        except ValueError:
            check(f"{name} refused", True)


def row(title, slot=False):
    return {"time": "10:00", "title": title, "person": "", "song_slot": slot}


def assigning():
    print("== persons into template rows ==")
    rows = [row("Klaviermusik"), row("Begrüßung"), row("Textlesung"), row("Lied", True), row("Gebet"),
            row("Missionsbericht"), row("Lied", True), row("Bibelgespräch"), row("Gebet"),
            row("Begrüßung zur Predigt"), row("Gebet für die Predigt"), row("Predigt"),
            row("Gebet des Predigers"), row("Segen"), row("Klaviermusik")]
    persons = [
        {"id": "begruessung1", "title": "Begrüßung", "person": "A"},
        {"id": "textlesung", "title": "Textlesung", "person": "B"},
        {"id": "gebet_knie", "title": "Gebet im Knien", "person": "C"},
        {"id": "missionsbericht", "title": "Missionsbericht", "person": "D"},
        {"id": "dankgebet", "title": "Dankgebet", "person": "E"},
        {"id": "bibelgespraech", "title": "Bibelgespräch", "person": "F"},
        {"id": "gebet_ende1", "title": "Gebet", "person": "G"},
        {"id": "begruessung2", "title": "Begrüßung", "person": "H"},
        {"id": "bekanntmachungen", "title": "Bekanntmachungen", "person": "I"},
        {"id": "gebet_predigt", "title": "Gebet für die Predigt", "person": "J"},
        {"id": "predigt", "title": "Predigt", "person": "K"},
        {"id": "gebet_abschluss", "title": "Gebet des Predigers", "person": "K"},
        {"id": "segen", "title": "Segenswort", "person": "K"},
        {"id": "klaviermusik", "title": "Klaviermusik", "person": ""},
    ]
    result, unassigned = assign_persons(rows, persons)
    check("rows matched in order", [r["person"] for r in result] == [
        "", "A", "B", "", "C", "D", "", "F", "G", "H", "J", "K", "K", "K", ""])
    check("points without a row reported (empty ones not)",
          [p["id"] for p in unassigned] == ["dankgebet", "bekanntmachungen"])
    check("template rows not changed", all(r["person"] == "" for r in rows))
    print("== bare book numbers ==")
    library = {
        "a.foil": {"title": "Lied A", "titles": ["Lied A"], "books": [{"name": "Gemeindelieder", "number": "101"}]},
        "b.foil": {"title": "Lied B", "titles": ["Lied B"], "books": [{"name": "Gemeindelieder", "number": "102"},
                                                                      {"name": "Jugendlieder", "number": "7"}]},
        "c.foil": {"title": "Lied C", "titles": ["Lied C"], "books": [{"name": "Jugendlieder", "number": "102"}]},
    }
    m = {r["line"]: r for r in match_lines(["101", "102"], library)}
    check("unique number found", m["101"]["song_id"] == "a.foil")
    check("number in two books left open", m["102"]["song_id"] is None and len(m["102"]["candidates"]) == 2)


def api():
    with TestClient(app) as client:
        print("== HTTP ==")
        check("nothing imported yet", client.get("/program/pdf").json()["ablauf"] is None)
        r = client.post("/program/pdf?filename=Gottesdienstablauf-20261126.pdf", content=make_pdf(FORM),
                        headers={"Content-Type": "application/pdf"})
        ablauf = r.json().get("ablauf", {})
        check("imported", r.status_code == 200 and ablauf["songs"] == ["101", "102", "Dir sei Ehre"]
              and ablauf["date"] == "2026-11-26" and ablauf["filename"] == "Gottesdienstablauf-20261126.pdf")
        check("not a PDF refused", client.post("/program/pdf", content=b"nope").status_code == 400)
        check("too large refused", client.post("/program/pdf", content=b"%PDF" + b"0" * (5 * 1024 * 1024)).status_code == 413)

        ids = [client.post("/program/items", json={"time": t, "title": title, "person": "", "group": ""}).json()["item"]["id"]
               for t, title in [("09:30", "Begrüßung"), ("09:35", "Lied"), ("10:30", "Predigt")]]
        tpl = client.post("/program/templates", json={"name": "GD", "slots": [ids[1]]}).json()["template"]
        persons = [{"id": p["id"], "person": p["person"]} for p in ablauf["points"]]
        r = client.post(f"/program/templates/{tpl['id']}/apply", json={"dry_run": True, "songs": [], "persons": persons})
        check("preview with persons", [x["person"] for x in r.json()["rows"]] == ["Anna Leiterin", "", "J. Prediger"]
              and "textlesung" in [p["id"] for p in r.json()["unassigned"]])
        r = client.post(f"/program/templates/{tpl['id']}/apply", json={"songs": [], "persons": persons})
        check("applied with persons", [i["person"] for i in r.json()["items"]] == ["Anna Leiterin", "", "J. Prediger"])
        check("unknown point refused", client.post(f"/program/templates/{tpl['id']}/apply", json={
            "songs": [], "persons": [{"id": "lied1", "person": "x"}]}).status_code == 400)

    with TestClient(app) as client:
        check("kept across restarts", (client.get("/program/pdf").json()["ablauf"] or {}).get("songs") == ablauf["songs"])


def main():
    parsing()
    assigning()
    api()
    print(f"\n{PASS} passed, {FAIL} failed")
    raise SystemExit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
