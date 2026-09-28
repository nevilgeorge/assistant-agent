# Gmail export — how to read it

A tree of exported Gmail messages, one JSON file per message. Answer questions with
`rg`, `jq`, and `python3`. Nothing here is specific to a particular account: derive the
sender list, labels, and date range from the data itself.

## Layout

```
<year>/<messageId>.json
```

The directory name is the year the message is from. It is a coarse bucket, not the
authoritative date — always read the date from the message (see **Dates** below). Each
file is a raw Gmail API `users.messages.get` response: `id`, `threadId`, `labelIds`,
`snippet`, `internalDate`, `sizeEstimate`, and a recursive `payload` of MIME parts.

## Never read the corpus into context

A typical export is thousands of files and hundreds of megabytes, and a single message
can exceed 100 KB of HTML. Reading even a few dozen raw files will exhaust the context
window and answer nothing.

Work through the index and the text corpus below. Read a raw `.json` file only when you
need a header or MIME detail the index does not carry, and then read one file, not a
directory. When you need many messages, write a Python script that streams them and
prints only what you need.

## Orient first

```sh
python3 build_index.py --inventory
```

Prints message count, true date range, thread counts, sent/bulk/attachment splits,
body-source mix, per-month volume, top senders, and attachment types. Run it before
answering anything that depends on scope.

## Build the index

```sh
python3 build_index.py            # writes .index/ beside the export
```

Takes a few seconds. `.index/` is derived data — safe to delete and rebuild, and the
builder skips dot-directories so it never reads its own output. Two artifacts:

**`.index/messages.jsonl`** — one row per message:

`id`, `thread_id`, `thread_size`, `thread_pos`, `year_dir`, `epoch_ms`, `date_utc`,
`date_local`, `date`, `weekday`, `utc_offset`, `from_name`, `from_email`, `to`, `cc`,
`reply_to`, `subject`, `snippet`, `labels`, `category`, `is_sent`, `is_inbox`,
`is_unread`, `is_starred`, `is_draft`, `list_id`, `is_bulk`, `in_reply_to`,
`has_references`, `size_estimate`, `mime_top`, `has_attachments`, `attachments`,
`body_source`, `body_chars`, `body_file`, `file`.

**`.index/bodies/<id>.txt`** — the message body as plain text, HTML stripped. This is
what makes full-text search usable; the raw JSON is mostly markup.

## Querying

Metadata with `jq`:

```sh
jq -r 'select(.date >= "2026-03-01" and .date < "2026-04-01" and .is_bulk == false)
       | "\(.date) | \(.from_email) | \(.subject)"' .index/messages.jsonl
```

Full text with `rg` over the corpus, then join back to metadata by message id:

```sh
rg -l -i 'confirmation number' .index/bodies/ \
  | xargs -n1 basename | sed 's/\.txt$//' | sort > /tmp/hits

python3 - <<'EOF'
import json
ids = set(open('/tmp/hits').read().split())
for line in open('.index/messages.jsonl'):
    r = json.loads(line)
    if r['id'] in ids:
        print(r['date'], r['from_email'], r['subject'])
EOF
```

`rg -l` prints matching paths; the basename is the message id, which joins straight
back to `messages.jsonl`. Use `rg -i -C2` instead of `-l` when you want the matching
lines rather than the list of messages.

Narrow by date or sender *before* grepping when you can — pass `rg` an explicit file
list built from the index rather than scanning all bodies.

## Parsing gotchas

**Bodies may be base64url-encoded.** `payload.body.data` and every nested part's
`body.data` arrive base64url-encoded from the API. A companion `decode_email_bodies.py`
rewrites them in place, and it is not idempotent or marked. Check one file before
assuming either state; the index builder handles whatever it finds.

**MIME parts nest recursively.** `payload.parts[].parts[]` can go several levels deep
(`multipart/mixed` wrapping `multipart/alternative` wrapping the text). Always walk the
tree; never assume the body is at `payload.body` or one level down.

**A `text/plain` part is often empty or a stub.** Preferring `text/plain` is right in
general — it is already clean text — but many senders ship a plain part that is empty,
or a bare token such as the brand name, beside a full HTML body. Taking it on faith
silently drops the message. Compare the two before choosing: across a real corpus the
ratio of HTML text to plain text is bimodal, with genuine alternatives under 1.5x and
stubs above 2x, so the index falls back to HTML whenever HTML yields more than twice
the text (`_STUB_RATIO`). Ticket, order, and booking confirmations are the usual
offenders, which makes this failure mode expensive: it hides exactly the messages that
answer "what did I book and when".

**HTML stripping is best-effort.** Marketing HTML is malformed constantly. When a
stripped body reads oddly, check the raw `text/html` part before concluding the message
said something strange. Note that void elements (`<meta>`, `<link>`, `<img>`) have no
closing tag — any tag-balancing logic must account for that or it will blank documents.

**Attachments are not in the export.** Parts with a `filename` carry only
`body.attachmentId`, never content. You can report names, MIME types, and sizes;
you cannot read a PDF, image, or `.ics` attachment. Inline `text/calendar` *parts*
(as opposed to `.ics` attachments) do carry data and are captured in the body text.

**Headers repeat.** `Received` appears once per hop; `DKIM-Signature` and ARC headers
repeat too. Build a header map that takes the last value, and never assume uniqueness.

**Labels are system-level.** `INBOX`, `SENT`, `DRAFT`, `UNREAD`, `STARRED`,
`IMPORTANT`, and `CATEGORY_*` are the Gmail defaults. User-created labels appear as
opaque `Label_<n>` ids whose display names live in a separate labels endpoint that is
typically *not* part of the export — do not guess at their meaning.

**`SENT` and `DRAFT` are mixed in.** Filter on `is_sent` / `is_draft` when counting
received mail, and remember sent messages are the best evidence of what the owner
actually said or committed to.

## Answering conventions

**Dates.** `internalDate` (epoch ms, UTC) is authoritative for ordering. The `Date`
header carries the sender's UTC offset, which is what a person means by when a message
arrived; the index's `date` and `date_local` prefer it and fall back to UTC. Name the
zone when a time matters, and do not silently normalize a travel-period message to the
home zone.

**Threads, not messages.** `thread_size` and `thread_pos` are on every row. A
twenty-message thread is one conversation — never report it as twenty. When summarizing
a thread, read the last message plus the first, not every message in between.

**Quoted history inflates matches.** Replies embed the entire message being replied to,
so a body match may be quoted text from days earlier rather than anything new. Before
citing a hit, confirm the phrase belongs to that message and not to its quoted tail.

**Bulk versus personal.** Use `is_bulk` (set from `List-Unsubscribe` / `List-Id`) and
`category` rather than guessing from the sender name. Newsletters and receipts usually
dominate the corpus by volume and will swamp any "how much mail did I get" answer if
left in. Say which filter you applied.

**Export-window caveat.** An export typically covers a trailing window of a few months,
and the earliest and latest months are often partial. Check the range in `--inventory`
before any month-over-month or "this year" comparison, and say when the window rather
than the owner's activity explains the shape of an answer.

**Corroborate with a sibling calendar export.** If the data root also holds a calendar
export, the two answer different halves of the same question: the calendar says what was
scheduled, the mail says what was booked, confirmed, cancelled, or paid for. A trip
missing from one is often fully documented in the other.

**Quote sparingly.** These are the owner's private messages. Pull the specific line that
answers the question rather than pasting whole bodies, and keep credentials, one-time
codes, and account numbers out of your replies even when they appear in a match.
