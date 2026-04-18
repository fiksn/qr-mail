# QR-mail

Simple tool that parses incoming mail for UPC QR codes and converts them to EPC QR format.
It is meant to be used with [https://gitlab.com/simple-nixos-mailserver/nixos-mailserver](https://gitlab.com/simple-nixos-mailserver/nixos-mailserver).
For trusted users a reply is sent back. `allowedSenders` will be processed, but never replied to - that
reply goes to `adminEmail`.

Add this to `flake.nix` inputs like this:
```
inputs = {
    qr-mail.url = "github:fiksn/qr-mail";
}
```
and then reference `qr-mail.nixosModules.default` in modules.

Configuration is like:
```
services.qrMail = {
   enable = true;
   myAddress = "qr@domain.tld";
   adminEmail = "admin@trusted-domain.tld";
   trustedSenders = [ "*@trusted-domain.tld" ];
   allowedSenders = [ "forwarding-noreply@google.com" ];
   catchAllWorkaround = true;
};
```
