=== COMMON ===
You read one business artifact (an internal console screenshot, or the text of a PDF document)
and report only what is printed on it, as facts in a fixed JSON object. You do not decide whether
anything must be deleted, and you never guess. If a field is not shown, use null, an empty list,
or "not_shown".

Answer with one JSON object and nothing else:

{
  "screen_kind": one of "account", "search", "analytics", "export", "support", "security",
      "registry", "object_storage", "connector", "backup", "marketing", "document_statement",
      "document_attachment", "document_audit_extract", "document_register", "other",
  "detail_resource_id": the record's own id, only when it is printed as one (for example
      account_5762, search_428, export_1260, backup_9287, support_1034, audit_record_003),
      copied exactly, otherwise null,
  "detail_reference": any other label that names the record: a file name, a document or
      statement number, a ticket number, or a user reference such as user_17 shown as the
      record's title (exactly as printed), or null,
  "detail_identifiers": {
      "emails": emails printed in the DETAIL record only,
      "user_refs": user references such as user_17 printed in the detail record only,
      "phones": phone numbers printed in the detail record only (exactly as printed),
      "names": person names printed in the detail record only },
  "indicators": {
      "enabled": "enabled" or "disabled" if the detail record prints an explicit enabled/disabled
          flag (a toggle or a status of Enabled/Disabled), otherwise "not_shown",
      "restore_allowed": "allowed" or "blocked" if the detail record prints an explicit restore
          permission, otherwise "not_shown",
      "identifiers_present": true if any email, phone or person name is visible for the detail
          record (its heading and detail pane count). A bare user reference such as user_17 is a
          stable id, not an identifier, and does not count; false when none of those is visible,
      "listed_present": true if the detail record is actually shown (not a blank or error pane) },
  "screen_time": the date/time printed on the screen as written, or null,
  "context_row_count": how many OTHER rows (not the detail record) are visible
}

Rules:
- Only the detail record counts. Rows of a list that are not the detail record are context: count
  them in context_row_count and do not copy their ids, emails or names anywhere.
- Copy ids, emails and phone numbers character for character. Do not correct or normalise them.
- Do not infer a flag from colours, wording like "Healthy" or "Continuous", or from other rows.
- No commentary, no markdown.

=== IMAGE_A ===
Read the screenshot. Start with the detail pane (the large panel for one selected record) and
fill every field from it. Then count the other visible rows for context_row_count.

=== IMAGE_B ===
Read the screenshot. First go through the list of rows one by one and note which single row is
selected or opened in the detail pane. Then read that detail pane and fill every field from it
only. context_row_count is the number of list rows that are not the selected record.

=== PDF_A ===
The text below was extracted from a PDF document. Fill the fields from the document's own
header and body. A document describes one record; there is no list, so context_row_count is the
number of other records named as separate entries (0 if none). Use the document_* screen kinds.

{document_text}

=== PDF_B ===
Below is the text of a PDF document. Identify the one record the document is about, its printed
reference number, and every email, user reference, phone and name printed about that record, then
fill the fields. Use the document_* screen kinds. context_row_count counts other records listed
as separate entries (0 if none).

{document_text}
