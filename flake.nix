{
  description = "qr-mail — Postfix pipe handler that forwards mail from allowed senders to an admin address";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems
        (system: f nixpkgs.legacyPackages.${system});
    in
    {
      nixosModules.default = import ./module.nix;

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = [
            (pkgs.python3.withPackages (ps: [
              ps.pillow # image loading / generation
              ps.pyzbar # QR code detection
              ps.pdf2image # PDF → image via poppler
              ps.pytesseract # OCR via tesseract
              ps.segno # QR code generation
              ps.defusedxml # safe XML parsing (XXE / billion laughs)
              ps.lxml # XML canonicalization for XMLDSig verification
              ps.cryptography # X.509 cert parsing + RSA signature verification
              ps.pytest # test runner
              ps.google-api-python-client # Gmail API
              ps.google-auth # service account credentials
            ]))
            pkgs.poppler-utils # pdfinfo + pdftoppm + pdftotext (runtime dep of pdf2image)
            pkgs.tesseract # OCR engine for text extraction from images
            pkgs.git # git
          ];
        };
      });
    };
}
