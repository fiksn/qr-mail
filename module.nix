{ config, lib, pkgs, ... }:

let
  cfg = config.services.qrMail;

  python = pkgs.python3.withPackages (ps: [
    ps.pillow # image loading
    ps.pyzbar # QR code detection (wraps ZBar)
    ps.pdf2image # renders PDF pages via poppler
    ps.pytesseract # OCR via tesseract
    ps.segno # QR code generation with ECI support
    ps.defusedxml # safe XML parsing (XXE / billion laughs)
    ps.lxml # XML canonicalization for XMLDSig verification
    ps.cryptography # X.509 cert parsing + RSA signature verification
    ps.google-api-python-client # Gmail API (gmail_fetch.py)
    ps.google-auth # service account credentials
  ]);

  # Bundle the Python packages and scripts into one store path.
  src = pkgs.runCommandLocal "qr-mail-src" { } ''
    mkdir -p $out
    cp -r ${./core} $out/core
    cp -r ${./parsers} $out/parsers
    cp -r ${./scripts} $out/scripts
    cp ${./upn_base_empty.jpg} $out/upn_base_empty.jpg
    cp ${./slo-intermediates.pem} $out/slo-intermediates.pem
  '';

  # Liberation Mono ships glyphs for Slovene diacritics (š/č/ž); plain Pillow on
  # NixOS otherwise falls back to its bitmap default, which renders them as tofu.
  monoFontRegular = "${pkgs.liberation_ttf}/share/fonts/truetype/LiberationMono-Regular.ttf";
  monoFontBold = "${pkgs.liberation_ttf}/share/fonts/truetype/LiberationMono-Bold.ttf";

  # Shell wrapper that sets env vars and invokes the Python script.
  # pdf2image calls pdftoppm at runtime, so poppler_utils must be on PATH.
  processorBin = pkgs.writeShellScriptBin "qr-mail-processor" ''
    export PATH="/run/wrappers/bin:${pkgs.poppler-utils}/bin:${pkgs.tesseract}/bin:$PATH"
    export PYTHONPATH=${src}
    export QR_MAIL_MONO_FONT_REGULAR=${monoFontRegular}
    export QR_MAIL_MONO_FONT_BOLD=${monoFontBold}
    export ADMIN_EMAIL=${lib.escapeShellArg cfg.adminEmail}
    export MY_ADDRESS=${lib.escapeShellArg cfg.myAddress}
    export ALLOWED_SENDERS=${lib.escapeShellArg (lib.concatStringsSep ":" cfg.allowedSenders)}
    export ALLOWED_SENDER_ROUTES=${lib.escapeShellArg (lib.concatStringsSep ":" cfg.allowedSenderRoutes)}
    export TRUSTED_SENDERS=${lib.escapeShellArg (lib.concatStringsSep ":" cfg.trustedSenders)}
    export MAX_ATTACHMENT_BYTES=${toString cfg.maxAttachmentBytes}
    export EPC_TO_UPN_CITY=${lib.escapeShellArg cfg.epcToUpnCity}
    export MAX_PDF_PAGES=${toString cfg.maxPdfPages}
    export PDF_RENDER_DPI=${toString cfg.pdfRenderDpi}
    export PDF_RENDER_TIMEOUT_S=${toString cfg.pdfRenderTimeoutSeconds}
    export PDFINFO_TIMEOUT_S=${toString cfg.pdfinfoTimeoutSeconds}
    export MAX_IMAGE_PIXELS=${toString cfg.maxImagePixels}
    export MAX_MESSAGE_RUNTIME_S=${toString cfg.maxMessageRuntimeSeconds}
    export SMTP_HOST=${lib.escapeShellArg cfg.smtpHost}
    export SMTP_PORT=${toString cfg.smtpPort}
    export SMTP_USER=${lib.escapeShellArg cfg.smtpUser}
    export SMTP_PASSWORD=${lib.escapeShellArg cfg.smtpPassword}
    export SMTP_TLS=${lib.escapeShellArg cfg.smtpTls}
    export SMTP_INSECURE_SKIP_VERIFY=${lib.boolToString cfg.smtpInsecureSkipVerify}
    export ESLOG_TRUSTED_CERTS_FILE=${lib.escapeShellArg cfg.eslogTrustedCertsFile}
    exec ${python}/bin/python3 ${src}/scripts/mail_processor.py "$@"
  '';
  gmailFetcherBin = pkgs.writeShellScriptBin "qr-mail-gmail-fetch" ''
    export PYTHONPATH=${src}
    export GMAIL_IMPERSONATE_ADDRESS=${lib.escapeShellArg cfg.gmailImpersonateAddress}
    export GMAIL_POLL_INTERVAL_S=${toString cfg.gmailPollIntervalSeconds}
    export GMAIL_PROCESSED_LABEL=${lib.escapeShellArg cfg.gmailProcessedLabel}
    export PROCESSOR_BIN=${processorBin}/bin/qr-mail-processor
    # Service account file path is passed at runtime via GMAIL_SERVICE_ACCOUNT_FILE
    # so that the secret never lands in the Nix store.
    exec ${python}/bin/python3 ${src}/scripts/gmail_fetch.py "$@"
  '';
in
{
  options.services.qrMail = {
    enable = lib.mkEnableOption "QR mail processor pipe handler";

    myAddress = lib.mkOption {
      type = lib.types.str;
      example = "test@example.com";
      description = ''
        The address that receives incoming mail and is used as the From address
        when forwarding. Must also be configured in mailserver.loginAccounts.
      '';
    };

    adminEmail = lib.mkOption {
      type = lib.types.str;
      example = "admin@example.com";
      description = "Address that forwarded mail is sent to.";
    };

    allowedSenders = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      example = [
        "*@trusted.com"
        "billing@vendor.tld"
      ];
      description = ''
        Glob patterns matched case-insensitively against the sender address.
        Mail from senders not matching any pattern is silently dropped
        (unless it matches trustedSenders).
      '';
    };

    allowedSenderRoutes = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      example = [
        "*@trusted.com=foo@bar.com,baz1@domain.com"
        "billing@vendor.tld=accounts@myco.tld"
      ];
      description = ''
        Optional per-sender routing rules for allowedSenders.

        Each entry is "<glob>=a@b.com,c@d.com". If the sender matches a route,
        the mail is sent to those recipients and adminEmail is CCed (admin is
        not included in To). The first matching route wins.
      '';
    };

    trustedSenders = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      example = [ "ceo@*" "*@partner.com" ];
      description = ''
        Like allowedSenders, but when matched the tool replies to the sender and
        CCs adminEmail. If an address matches both lists, trustedSenders wins.
      '';
    };

    maxAttachmentBytes = lib.mkOption {
      type = lib.types.int;
      default = 104857600; # 100 MB
      description = "Maximum size in bytes of a single attachment to scan for QR codes.";
    };

    maxPdfPages = lib.mkOption {
      type = lib.types.int;
      default = 10;
      description = "Maximum number of PDF pages to render and scan per attachment.";
    };

    pdfRenderDpi = lib.mkOption {
      type = lib.types.int;
      default = 200;
      description = "DPI for PDF→image rendering. 150 misses small QR codes; 200 is the minimum for reliable detection.";
    };

    pdfRenderTimeoutSeconds = lib.mkOption {
      type = lib.types.int;
      default = 20;
      description = "Timeout in seconds for rendering a single PDF attachment (pdf2image/poppler).";
    };

    pdfinfoTimeoutSeconds = lib.mkOption {
      type = lib.types.int;
      default = 3;
      description = "Timeout in seconds for pdfinfo when checking if a PDF is encrypted.";
    };

    maxImagePixels = lib.mkOption {
      type = lib.types.int;
      default = 40000000;
      description = "Maximum image pixel count allowed when scanning (PIL decompression bomb guard).";
    };

    maxMessageRuntimeSeconds = lib.mkOption {
      type = lib.types.int;
      default = 60;
      description = "Maximum total processing time in seconds per message (scan+parse+build).";
    };

    catchAllWorkaround = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Enable when the mailserver uses a catch-all rule that intercepts
        <option>myAddress</option> before Postfix transport rules can match it.

        Adds <option>myAddress</option> to <literal>mailserver.extraVirtualAliases</literal>
        pointing to <literal>qr-mail-pipe@localhost</literal>, and routes that
        address to the pipe via the transport map. Because the alias lands in
        the same virtual alias map file as the catch-all, Postfix resolves the
        specific address first and the pipe receives the mail correctly.

        Requires nixos-mailserver (simple-nixos-mailserver).
      '';
    };

    epcToUpnCity = lib.mkOption {
      type = lib.types.str;
      default = "Ljubljana";
      description = ''
        Recipient city used when converting an EPC QR code to UPN format.
        EPC does not carry a recipient city, which is mandatory in UPN.
      '';
    };

    smtpHost = lib.mkOption {
      type = lib.types.str;
      default = "";
      description = ''
        SMTP server hostname. When set, outbound mail is sent via SMTP instead
        of the local sendmail binary. Leave empty to use sendmail (default).
        Ignored when <option>gmailServiceAccountFile</option> is set.
      '';
    };

    smtpPort = lib.mkOption {
      type = lib.types.port;
      default = 587;
      description = "SMTP server port (default: 587 for STARTTLS).";
    };

    smtpUser = lib.mkOption {
      type = lib.types.str;
      default = "";
      description = "SMTP username for authentication. Leave empty to skip auth.";
    };

    smtpPassword = lib.mkOption {
      type = lib.types.str;
      default = "";
      description = ''
        SMTP password for authentication.

        Warning: this value lands in the Nix store as world-readable plain text.
        For production use, supply the password via an environment file outside
        the store (e.g. sops-nix or systemd EnvironmentFile).
      '';
    };

    smtpTls = lib.mkOption {
      type = lib.types.enum [ "starttls" "tls" "none" ];
      default = "starttls";
      description = ''
        TLS mode for outbound SMTP:
        - <literal>starttls</literal>: STARTTLS upgrade on connect (default, port 587)
        - <literal>tls</literal>: implicit TLS / SMTPS (port 465)
        - <literal>none</literal>: plain SMTP, no encryption
      '';
    };

    smtpInsecureSkipVerify = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Disable SMTP certificate and hostname verification.

        This weakens transport security and should only be used for broken
        internal SMTP servers that cannot be fixed properly.
      '';
    };

    eslogTrustedCertsFile = lib.mkOption {
      type = lib.types.str;
      default = "";
      example = "/etc/ssl/certs/eslog-extra-trust.pem";
      description = ''
        Optional PEM bundle of additional trust anchors for eSLOG XML
        signature verification. System trust roots are always used as the
        baseline; this file lets you add private or missing CAs.
      '';
    };

    gmailServiceAccountFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      example = "/run/secrets/qr-mail-service-account.json";
      description = ''
        Path to a Google service account JSON key file with domain-wide
        delegation enabled for the Gmail API. When set, a polling daemon
        (<literal>qr-mail-gmail-fetch</literal>) is started and outbound mail
        is sent through the Gmail API using the same impersonated mailbox,
        instead of Postfix/sendmail or SMTP.

        The file must be readable by the <literal>qr-mail</literal> system
        user. Use a secrets manager (e.g. sops-nix) so the key does not land
        in the Nix store.
      '';
    };

    gmailImpersonateAddress = lib.mkOption {
      type = lib.types.str;
      default = "";
      example = "qr@yourdomain.com";
      description = ''
        Google Workspace address the service account impersonates via
        domain-wide delegation. Must match the inbox that receives payment
        mail.
      '';
    };

    gmailPollIntervalSeconds = lib.mkOption {
      type = lib.types.int;
      default = 60;
      description = "Seconds between Gmail inbox polls.";
    };

    gmailProcessedLabel = lib.mkOption {
      type = lib.types.str;
      default = "qr-mail-processed";
      description = ''
        Gmail label applied to messages after successful processing.
        The label is created automatically on first run if it does not exist.
        It is hidden from the label list to keep the inbox tidy.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    users.users.qr-mail = {
      isSystemUser = true;
      group = "qr-mail";
      description = "qr-mail Postfix pipe user";
    };
    users.groups.qr-mail = { };

    services.postfix.settings.master."qr-mail" = lib.mkIf (cfg.gmailServiceAccountFile == null) {
      type = "unix";
      privileged = true;
      chroot = false;
      command = "pipe";
      args = [
        "flags=Rq"
        "user=qr-mail"
        "argv=${processorBin}/bin/qr-mail-processor" "\${sender}"
      ];
    };

    mailserver.extraVirtualAliases =
      lib.mkIf (cfg.catchAllWorkaround && cfg.gmailServiceAccountFile == null) {
        "${cfg.myAddress}" = "qr-mail-pipe@localhost";
      };

    services.postfix.transport = lib.mkIf (cfg.gmailServiceAccountFile == null)
      (if cfg.catchAllWorkaround
       then "qr-mail-pipe@localhost  qr-mail:\n"
       else "${cfg.myAddress}  qr-mail:\n");

    systemd.services.qr-mail-gmail-fetch = lib.mkIf (cfg.gmailServiceAccountFile != null) {
      description = "qr-mail Gmail fetch daemon";
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      wantedBy = [ "multi-user.target" ];
      serviceConfig = {
        Type = "simple";
        User = "qr-mail";
        Group = "qr-mail";
        ExecStart = "${gmailFetcherBin}/bin/qr-mail-gmail-fetch";
        # Service account file is injected here so it never enters the Nix store.
        Environment = "GMAIL_SERVICE_ACCOUNT_FILE=${cfg.gmailServiceAccountFile}";
        Restart = "on-failure";
        RestartSec = "30s";
        # Hardening
        NoNewPrivileges = true;
        PrivateTmp = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        ReadOnlyPaths = [ cfg.gmailServiceAccountFile ];
      };
    };
  };
}
