"""src.chat — chat-graph metadata utilities (TU-1 of Closure A plan_v3).

Single-source helper module for chat-derived FactRow metadata. All chat
extractors MUST call `metadata_builder._build_chat_metadata` rather than
constructing metadata inline (per architect arch-1 + AC-U9-5 helper-uniqueness
grep).
"""
