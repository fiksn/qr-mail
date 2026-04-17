{
  description = "qr-mail — Postfix pipe handler that forwards mail from allowed senders to an admin address";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }: {
    nixosModules.default = import ./module.nix;
  };
}
