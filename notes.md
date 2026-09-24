- **Line breaks in plain-text messages are kept.** The HTML body (what Beeper and other clients display) left newlines as
  newlines, which HTML treats as spaces, so every multi-line message arrived as one run-on paragraph. They now become
  `<br/>`; the text is still escaped, the plain `body` is unchanged, and `html=1` messages are passed through as before.
- **No more `ConnectionResetError` tracebacks in the journal.** A client dropping an idle keep-alive connection (bbctl's
  appservice proxy does it after almost every event - hundreds a week) is routine, and is now logged at DEBUG only.
  Any other error still gets its full traceback.
- README: new section *How a message looks: plain text and HTML* - which field shows where, escaping, `html=1`,
  emoji vs `:shortcodes:` and Markdown.

