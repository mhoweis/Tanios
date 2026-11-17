# Voice notes

Send Tanios a voice note and it answers with a voice note. Nothing new needs setting
up: voice uses the same OpenRouter key as chat, and it switches on with this update.

## What happens when you send a voice note

1. Tanios downloads the recording from WhatsApp.
2. A speech model turns it into text.
3. The answer is written the same way as for a typed question, but worded to be heard.
4. A voice model reads the answer aloud, and Tanios sends that as a voice note.

Some details worth knowing:

- **Things to copy also come as text.** If the answer holds a link, an email address, a
  long number or a list, you get the voice note and a text message.
- **Long answers are spoken in part.** About the first 90 seconds are read aloud, and
  the full answer follows as text.
- **Spoken commands work.** Say "status" or "pause" and Tanios acts on it. The reply
  to a command is text, because it is a list or figures.
- **If the voice reply fails,** the answer still arrives as text.
- **Recordings over five minutes** are cut at five minutes.
- **English only** for now, as in the rest of version 1.

## Change the voice

The voice is one line in the models file. Follow "Change a model" in
[MODELS-AND-BUDGET.md](MODELS-AND-BUDGET.md) to make your own copy of the file, then in
the `[[speech.speak]]` section change:

```toml
voice = "alloy"
```

The voices a model offers are listed on its page at https://openrouter.ai. Save, then:

```sh
cd /opt/tanios
docker compose restart app
```

A different voice model can be used the same way, by changing the `model` line. Some
voice models only return one audio format; if a new one does not work, try
`format = "pcm"` instead of `format = "mp3"`.

## What it costs

A voice note uses three models instead of one, and all three count toward your
monthly cap:

| Step | Cost |
| --- | --- |
| Voice to text | A tiny fraction of a cent. The default model was $0.000003 a second on 15 November 2026. |
| The answer | The same as a typed question. |
| Text to voice | Usually the largest of the three. |

Tanios cannot see the exact price of the text-to-voice step, because the provider
sends back audio with no cost attached. The figure in `status` for `speak` is an
estimate from the price in the models file. To see the real amount, open **Activity**
on openrouter.ai, and correct `price_per_million_chars` in your models file if it is
far off.

Each voice reply is also one WhatsApp message, or two when text is sent with it.

After you reach your monthly cap, voice notes stop until the 1st of next month. Typed
messages and commands keep working.

## The checks for milestone 3

| Check | Do this | You should see |
| --- | --- | --- |
| W2 | Send a voice note asking a simple question | A voice note with the answer, and no text. |
| W2 | Send a voice note asking for a website address, such as "what is the web address of Wikipedia?" | A voice note, and the same answer as text. |
| W2 | Send a voice note saying only "status" | The status text, with `transcribe` in the list. |
| R1 | Send `status` after the checks above | Lines for `transcribe`, `chat` and `speak`. |

The spec's check for W2 asks for a reminder note by voice and then finding that note.
Notes arrive with memory in milestone 4, so that part is checked then.

## If something is wrong

| What you see | What to do |
| --- | --- |
| `Voice notes are not set up on this server yet.` | The `[speech]` section is missing from your models file, or its provider has no key. Compare your `models.local.toml` with `models.toml`. |
| `Sorry, I could not process that voice note.` | Run `docker compose logs app` and look for `transcription failed` or `voice note not understood`. |
| The answer comes as text instead of voice | Look in the log for `voice reply not sent`. The usual cause is a voice name the model does not have. |
| The voice note shows a download arrow instead of a play button | WhatsApp does that for files over 512 KB. Tell Claude; replies are meant to stay well under it. |
