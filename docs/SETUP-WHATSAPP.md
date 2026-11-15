# Connecting WhatsApp and a model

After this guide you can text Tanios on WhatsApp and get an answer. Allow about an hour.

Do [DEPLOY.md](DEPLOY.md) first. Meta will only talk to a public `https://` address, so
`https://YOUR_DOMAIN/healthz` has to work before you start here.

You will collect seven values and put them in the `.env` file on the server. Keep a
note open and paste each one as you get it. They are secrets: do not share them, and
never paste them into a chat with Claude.

Meta changes its dashboard often. If a button is not where this guide says, Meta's own
walkthrough is here:
https://developers.facebook.com/documentation/business-messaging/whatsapp/get-started

## Part 1: a model key from OpenRouter

1. Go to https://openrouter.ai and create an account.
2. Open **Credits** and add a small amount. A few dollars is plenty to start.
3. Open **Keys**, choose **Create key**, and give it the name `tanios`. If the form
   offers a credit limit, set a small one, for example 5 dollars.
4. Copy the key. It starts with `sk-or-`. This is `TANIOS_OPENROUTER_API_KEY`.

The model that answers is named in `TANIOS_DEFAULT_MODEL`. The `.env.example` file
suggests a small, cheap one. You can replace it with any model ID from
https://openrouter.ai/models at any time.

## Part 2: a WhatsApp number from Meta

### 2.1 Create the app

1. Go to https://developers.facebook.com and log in with a Facebook account.
2. Open **My Apps** and choose **Create App**.
3. When asked what the app is for, pick the option about connecting with customers
   through **WhatsApp**. Create or select a business portfolio when asked.

### 2.2 The number and its ID

1. In the app, open **WhatsApp**, then **API Setup**.
2. Meta gives you a **test phone number** for free. Use it to begin; adding a number
   of your own can wait.
3. Under the test number, copy the **Phone number ID**. It is a long number, not the
   phone number itself. This is `TANIOS_WHATSAPP_PHONE_NUMBER_ID`.
4. In the **To** box on the same page, add your own WhatsApp number and confirm the
   code Meta sends you. The test number can only talk to numbers added here.

### 2.3 The app secret

1. Open **App settings**, then **Basic**.
2. Next to **App secret**, choose **Show** and copy it. This is
   `TANIOS_WHATSAPP_APP_SECRET`. Tanios uses it to prove each message really came
   from Meta.

### 2.4 A token that does not expire

The token shown on the API Setup page stops working after a short time. Make a
permanent one:

1. Go to https://business.facebook.com/latest/settings and open **System users**.
2. Choose **Add** and create a system user named `tanios`.
3. Select it and choose **Assign assets**. Give it **Manage app** on your app, and
   **Manage WhatsApp Business accounts** on your WhatsApp account.
4. Choose **Generate token**. Tick these three permissions: `business_management`,
   `whatsapp_business_messaging`, `whatsapp_business_management`.
5. Copy the token. This is `TANIOS_WHATSAPP_ACCESS_TOKEN`.

## Part 3: give the values to the server

Connect to the server and open the settings file:

```sh
ssh root@YOUR_SERVER_IP
cd /opt/tanios
git pull
echo "Verify token to use: $(openssl rand -hex 24)"
nano .env
```

If `.env` has no WhatsApp lines yet, copy them from `.env.example`. Fill in:

| Setting | Value |
| --- | --- |
| `TANIOS_WHATSAPP_VERIFY_TOKEN` | the text that was just printed |
| `TANIOS_WHATSAPP_APP_SECRET` | from step 2.3 |
| `TANIOS_WHATSAPP_ACCESS_TOKEN` | from step 2.4 |
| `TANIOS_WHATSAPP_PHONE_NUMBER_ID` | from step 2.2 |
| `TANIOS_OWNER_NUMBER` | your own WhatsApp number with country code, digits only |
| `TANIOS_OPENROUTER_API_KEY` | from part 1 |
| `TANIOS_DEFAULT_MODEL` | leave the suggested value, or choose your own |

Save with Ctrl+O, Enter, then exit with Ctrl+X. Restart:

```sh
docker compose up -d --build
```

Open `https://YOUR_DOMAIN/readyz`. It should now end with `"whatsapp":"on"`. If it says
`"off"`, run `docker compose logs app` and look for the line that starts
`WhatsApp chat is off. Still to set:`. It names the settings that are still empty.

## Part 4: tell Meta where Tanios lives

1. Back in the Meta app, open **WhatsApp**, then **Configuration**.
2. Under **Webhook**, choose **Edit** and enter:
   - Callback URL: `https://YOUR_DOMAIN/webhooks/whatsapp`
   - Verify token: the same text you put in `TANIOS_WHATSAPP_VERIFY_TOKEN`
3. Choose **Verify and save**. Meta calls Tanios once to check the token.
4. In the list of webhook fields, switch on **messages**.

## Part 5: the checks

From your own phone, message the test number.

| Check | Do this | You should see |
| --- | --- | --- |
| W1 | Send `what can you do?` | A real answer within a few seconds. |
| W4 | Send `help` | The list of commands. |
| W4 | Send `status` | `Tanios is active.` |
| W4 | Send `pause`, then any question | It says it is paused and does not answer the question. |
| W4 | Send `resume`, then the question again | It answers. |
| W3 | From a different phone that you also added in step 2.2, send `hello` twice | One line saying this is a private assistant, then nothing. |

When all six pass, milestone 1 is done. Tell Claude, and milestone 2 starts.

## What this costs

- **OpenRouter:** each answer costs a fraction of a cent on a small model, taken from
  your credits.
- **Meta:** since 1 October 2026 each business number gets 1,000 free replies a month,
  and replies beyond that are charged
  ([Meta's pricing page](https://developers.facebook.com/documentation/business-messaging/whatsapp/pricing)).
  Every message Tanios sends counts as one reply, including `Working on it...`.

## If something is wrong

| What you see | What to do |
| --- | --- |
| Meta says the callback URL could not be verified | Check `/readyz` shows `"whatsapp":"on"`, and that the verify token in Meta matches `.env` exactly. |
| You message the number and nothing comes back | Check the **messages** field is switched on (part 4). Then `docker compose logs app`. |
| Your own number gets the "private assistant" line | `TANIOS_OWNER_NUMBER` does not match your number. Use digits only, with country code, no `+` or spaces needed. If you use a WhatsApp username with your number hidden, Meta does not send Tanios your number on first contact; turn that off for the first message. |
| The log says `sending a WhatsApp message failed: HTTP 401` | The access token is wrong or expired. Repeat step 2.4. |
| The log says `model call failed: HTTP 401` or `402` | The OpenRouter key is wrong, or the credits have run out. |
| Tanios answers `Sorry, I could not get an answer just now` | The model ID in `TANIOS_DEFAULT_MODEL` may not exist any more. Pick another on openrouter.ai/models. |

## Known limits in this milestone

- Tanios does not remember earlier messages yet. Each question is answered alone.
  Memory arrives in milestone 4.
- `status` does not show spend yet. That arrives with the budget in milestone 2.
- Voice notes get a short "text only for now" reply until milestone 3.
- Meta's test number is for trying things out. Moving to a number of your own is a
  later step and comes with its own guide.
