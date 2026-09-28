# Google Calendar export — how to read it

A tree of exported Google Calendar events, one JSON file per event. Answer
questions about them with `rg`, `jq`, and `python3`. Nothing here is specific to a
particular account: derive the calendars, people, and date range from the data itself.

## Layout

```
<batch>/<sha256(calendarId)>/<sha256(eventId)>.json
```

Each file is:

```json
{"calendarId": "...", "event": { <raw Google Calendar API event> }}
```

Directory names are opaque hashes — never infer meaning from them, and never assume a
top-level folder is a strict date filter. A folder named for a year routinely holds
events from the tail of the previous year and the start of the next.

## Orient first

```sh
python3 build_index.py --inventory
```

Prints every calendar with its display name, event count, true date range, event-type
mix, timezones, the user's own RSVP spread, and the most frequent attendees. Run this
before answering anything that depends on scope — which calendars exist, who recurs,
how far the data actually reaches.

A calendar's human name is on `.event.organizer.displayName` for events it owns
(`organizer.email == calendarId`). A user's own primary calendar usually has no display
name, just the account email — that email identifies the calendar owner.

## The index

```sh
python3 build_index.py "$SCRATCHPAD/events.jsonl"
```

One JSONL row per event with normalized fields: `date`, `weekday`, `end_date`,
`all_day`, `start`, `start_time`, `end_time`, `tz`, `duration_min`, `calendar`,
`cal_id`, `summary`, `location`, `description`, `event_type`, `status`,
`transparency`, `organizer`, `creator`, `attendees`, `n_attendees`, `self_response`,
`recurring`, `hangout`, `id`, `ical_uid`, `file`.

Build into a scratchpad, not the repo. Rebuild rather than hand-editing — the JSON
files are the source of truth. Typical query:

```sh
jq -r 'select(.date >= "2026-09-21" and .date <= "2026-09-27")
       | "\(.date) \(.start_time // "all-day") \(.tz // "") | \(.calendar) | \(.summary)"' \
  "$SCRATCHPAD/events.jsonl"
```

## Parsing gotchas

**All-day vs timed.** Timed events have `start.dateTime` (with UTC offset) and
`start.timeZone`. All-day events have `start.date` and **no timezone**. Always branch
on which key is present.

**All-day end dates are exclusive.** A one-day all-day event on the 5th ends on the
6th. Subtract a day before reporting a last day or you will overstate every multi-day
trip by 24 hours. The index's `end_date` is already inclusive.

**Recurring events.** An export may contain either expanded instances or the series
master. Instances carry `recurringEventId` and `originalStartTime`; a master carries an
RRULE in `recurrence`. Check which before counting — if masters are present you must
expand them yourself, and if only instances are present there is no series to expand.

**Cancellations.** `status` may be `cancelled`; such events still appear in the export.
Filter them out of "what happened" answers unless the question is about cancellations.

**`eventType`.** `default` is a real event. `fromGmail` is auto-generated from an inbox
confirmation — flights, hotel stays, restaurant and appointment bookings. `birthday`,
`outOfOffice`, `focusTime`, and `workingLocation` are also possible. Exclude
`fromGmail` from "how busy was I" and meeting counts; it is the best source for travel
and dining questions. Treat `transparency: transparent` as not blocking time.

**Attendance.** The owner's own row in `attendees` has `self: true`; its
`responseStatus` (`accepted` / `declined` / `tentative` / `needsAction`) says whether
they actually went. A `declined` event is on the calendar but was not attended. Events
with no `attendees` array are solo or informational.

## Answering conventions

**Timezones.** Report times in the event's own timezone and name the zone — "3:00 PM
PT", not a bare "3:00 PM". `start.dateTime` already carries the right offset;
`start.timeZone` names it. Expect clusters of a non-home zone during travel; do not
silently normalize them to one zone.

**Deduplicate across calendars.** When someone keeps several calendars — a personal one
plus a shared household or partner calendar — the same real-world event often appears on
both under different titles. Never report the union as distinct events.

Time overlap alone is *not* sufficient evidence: back-to-back and genuinely concurrent
events overlap constantly, and a long all-day or evening block will collide with
everything around it. Use overlap only to generate candidates, then confirm with title
similarity, shared venue, or shared people before merging. Two patterns worth knowing:

- A `fromGmail` reservation frequently backs a hand-written event on another calendar
  (a restaurant booking under the dinner it belongs to). Same outing — count once, and
  prefer the human-written title.
- The same outing titled from each side ("dinner with X" vs "X / Y") is one event.

When you do merge, say so, so the user can see what you collapsed.

**Name prefixes on shared calendars.** Shared calendars commonly prefix a title with a
first name to scope the event to one person; unprefixed entries are joint. Confirm the
convention against the inventory's attendee list before relying on it.

**Export-window caveat.** Calendars in one export rarely cover the same span — a shared
calendar may start months after the personal one, and the most recent months are often
thin. Check the ranges in `--inventory` before any month-over-month, year-over-year, or
"all year" comparison, and state the caveat when the window, not the user's actual
activity, explains the shape of the answer.
