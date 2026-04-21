/**
 * Google Apps Script — Email sender webhook for mw-backend password resets.
 *
 * SETUP (one-time):
 *   1. Go to https://script.google.com and create a new project.
 *   2. Paste this entire file into the editor (replace any existing code).
 *   3. Click Deploy → New deployment → Web app.
 *      - Execute as: Me
 *      - Who has access: Anyone
 *   4. Copy the web app URL and set it as GAS_WEBHOOK_URL in your .env file.
 *
 * Accepts POST with JSON body:
 *   { to, subject, body, htmlBody? }
 * Sends plain-text email (+ optional HTML version) from your Google account.
 */

function doPost(e) {
  try {
    var payload  = JSON.parse(e.postData.contents);
    var to       = payload.to;
    var subject  = payload.subject  || "Message from michaelwegter.com";
    var body     = payload.body     || "";
    var htmlBody = payload.htmlBody || null;

    if (!to) {
      return ContentService
        .createTextOutput(JSON.stringify({ error: "Missing 'to' field" }))
        .setMimeType(ContentService.MimeType.JSON);
    }

    var options = {};
    if (htmlBody) options.htmlBody = htmlBody;

    GmailApp.sendEmail(to, subject, body, options);

    return ContentService
      .createTextOutput(JSON.stringify({ ok: true }))
      .setMimeType(ContentService.MimeType.JSON);

  } catch (err) {
    return ContentService
      .createTextOutput(JSON.stringify({ error: err.message }))
      .setMimeType(ContentService.MimeType.JSON);
  }
}

// Optional: test by running this function manually in the Apps Script editor
function _testSend() {
  doPost({ postData: { contents: JSON.stringify({
    to: "your-email@example.com",
    subject: "Test from GAS",
    body: "It works!",
    htmlBody: "<p>It <strong>works!</strong></p>"
  })}});
}
