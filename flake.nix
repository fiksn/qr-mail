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
              ps.segno # QR code generation
              ps.pytest # test runner
              ps.google-api-python-client # Gmail API
              ps.google-auth # service account credentials
            ]))
            pkgs.poppler-utils # pdfinfo + pdftoppm (runtime dep of pdf2image)
            pkgs.git # git
          ];
        };
      });
    };
}
