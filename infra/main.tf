terraform {
  required_providers {
    descope = {
      # Uses the fork at jamescrowley321/terraform-provider-descope.
      # To use the fork locally, build it and configure dev_overrides:
      #
      #   cd ~/repos/terraform-provider-descope && make dev
      #
      # This installs the binary to $GOPATH/bin and creates ~/.terraformrc
      # with dev_overrides pointing descope/descope to the local build.
      source = "descope/descope"
    }
  }
}

provider "descope" {}
