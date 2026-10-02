# Skill: batch QC before analysis

Use this before comparing a compound with vehicle across more than one plate or batch.

1. Parse every readings workbook with `parse_xlsx`, and the protocol with `extract_protocol_fields`.
2. Run `check_consistency`. It flags plates whose treatment time or vehicle wells don't match the protocol.
3. For each open contradiction, ask the owners. The owner of the data file decides. Offer the choices the contradiction lists.
4. Don't run `filter_dataset` or `run_stats` while a contradiction is open. The guardrails block it anyway.
5. Run `filter_dataset`, then `run_stats`. Each plate is compared with its own vehicle wells.
6. If a notebook reached a different conclusion, say which step differs (e.g. pooling plates without vehicle), and attach a note for its owner.
7. Report "no evidence of an effect in this data", never "the compound doesn't work".
