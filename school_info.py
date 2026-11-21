"""School announcements, personal absences, and timetable lookups."""

import json
import urllib.parse
import urllib.request
from datetime import timedelta
from html.parser import HTMLParser

import legacy_bot as untis


class SchoolInfoUnavailable(Exception):
    """An expected lookup failure with a message suitable for Discord."""


class AnnouncementText(HTMLParser):
    breaks = {"br", "div", "li", "p", "section", "tr", "ul", "ol"}

    def __init__(self):
        super().__init__()
        self.parts = []
        self.link = None
        self.hidden = None

    def handle_starttag(self, tag, attrs):
        if self.hidden:
            return
        if tag in {"script", "style"}:
            self.hidden = tag
        elif tag in self.breaks:
            self.parts.append("\n")
        elif tag == "a":
            href = dict(attrs).get("href") or ""
            if href.startswith(("https://", "http://")) and not any(char.isspace() for char in href):
                self.link = href

    def handle_endtag(self, tag):
        if self.hidden:
            if tag == self.hidden:
                self.hidden = None
        elif tag in self.breaks:
            self.parts.append("\n")
        elif tag == "a" and self.link:
            if not self.parts or self.parts[-1].strip() != self.link:
                self.parts.append(f" ({self.link})")
            self.link = None

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def announcement_text(value):
    parser = AnnouncementText()
    parser.feed(str(value or ""))
    parser.close()
    return "\n".join(line.strip() for line in "".join(parser.parts).splitlines() if line.strip())


def web_records(opener, base_url, path, params, key):
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(f"{base_url}api/{path}?{query}", headers={"Accept": "application/json"})
    with opener.open(request, timeout=20) as response:
        payload = json.load(response)
    data = payload.get("data") if isinstance(payload, dict) else None
    records = data.get(key) if isinstance(data, dict) else None
    if not isinstance(records, list) or not all(isinstance(item, dict) for item in records):
        raise RuntimeError(f"Unexpected WebUntis response for {key}.")
    return records


def personal_absences(opener, base_url, auth, start, end):
    if str(auth.get("personType", 5)) != "5":
        raise SchoolInfoUnavailable("The linked WebUntis account is currently set up for a non-student role.")
    return web_records(opener, base_url, "classreg/absences/students", {
        "studentId": auth["personId"], "startDate": untis.normal_date(start),
        "endDate": untis.normal_date(end), "excuseStatusId": -1,
    }, "absences")


def fetch_timetable(opener, endpoint, resource_id, resource_type, start, end):
    options = {
        "id": resource_id,
        "element": {"id": resource_id, "type": resource_type},
        "startDate": untis.normal_date(start),
        "endDate": untis.normal_date(end),
        "showLsText": True,
        "showStudentgroup": True,
        "showLsNumber": True,
        "showSubstText": True,
        "showInfo": True,
        "showBooking": True,
    }
    return untis.records_from(untis.rpc(opener, endpoint, "getTimetable", {"options": options}))


def resource_aliases(resource, resource_type):
    name = str(resource.get("name") or "").strip()
    long_name = str(resource.get("longName") or resource.get("longname") or "").strip()
    first_name = str(resource.get("foreName") or resource.get("firstname") or "").strip()
    if resource_type == 5 and first_name:
        display_name = f"{first_name} {name or long_name}".strip()
        return (display_name, f"{name or long_name} {first_name}")
    return (name, long_name)


def find_timetable_resource(opener, endpoint, method, resource_type, query, monday):
    resources = untis.rpc(opener, endpoint, method, {})
    if not isinstance(resources, list):
        raise RuntimeError("Unexpected WebUntis timetable directory response.")
    wanted = " ".join(query.split()).casefold()
    matches = []
    for resource in resources:
        if not isinstance(resource, dict) or not resource.get("id"):
            continue
        aliases = resource_aliases(resource, resource_type)
        if wanted and any(wanted == " ".join(alias.split()).casefold() for alias in aliases):
            matches.append((resource["id"], aliases[0] or aliases[1]))
    if len(matches) != 1:
        return None
    resource_id, label = matches[0]
    return label, fetch_timetable(opener, endpoint, resource_id, resource_type, monday, monday + timedelta(days=4))


def timetable_lookup(opener, endpoint, student, class_name, monday):
    student = (student or "").strip()
    class_name = (class_name or "").strip()
    if not (student or class_name):
        raise SchoolInfoUnavailable("Enter a student's full name or a class name.")
    if student:
        try:
            result = find_timetable_resource(opener, endpoint, "getStudents", 5, student, monday)
        except untis.WebUntisApiError as error:
            if error.code != "-8509":
                raise
        else:
            if result is not None:
                return (*result, False)

    if class_name:
        result = find_timetable_resource(opener, endpoint, "getKlassen", 1, class_name, monday)
        if result is not None:
            return (*result, bool(student))
        raise SchoolInfoUnavailable("I couldn't match that class. Enter its exact WebUntis name.")
    raise SchoolInfoUnavailable("I couldn't look up that student. Provide their class to see the class timetable.")
