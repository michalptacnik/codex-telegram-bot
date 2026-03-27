use anyhow::Context;
use std::io::{self, Write};
use zeroclaw::channels::email_channel::EmailChannel;
use zeroclaw::config::Config;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let recipient = std::env::args()
        .nth(1)
        .unwrap_or_else(|| "michalptacnik@gmail.com".to_string());

    let config = Config::load_or_init().await?;
    let email = config
        .channels_config
        .email
        .clone()
        .context("channels_config.email is not configured")?;

    let channel = EmailChannel::new(email);
    let nonce = uuid::Uuid::new_v4().simple().to_string();
    let subject = format!("ZeroClaw mail smoke {nonce}");
    let body = format!("ZeroClaw mail smoke test {nonce}");
    let message_id = format!("<zeroclaw-mail-smoke-{nonce}@localhost>");

    let receipt = channel.send_email(&recipient, Some(&subject), &body, Some(&message_id))?;
    println!(
        "{}",
        serde_json::to_string_pretty(&serde_json::json!({
            "step": "send",
            "receipt": receipt,
        }))?
    );
    io::stdout().flush()?;

    let verified = tokio::time::timeout(
        std::time::Duration::from_secs(20),
        channel.verify_sent_message(&message_id),
    )
    .await
    .context("verify_sent timed out")??;
    println!(
        "{}",
        serde_json::to_string_pretty(&serde_json::json!({
            "step": "verify_sent",
            "message_id": message_id,
            "verified": verified.is_some(),
            "message": verified,
        }))?
    );

    Ok(())
}
