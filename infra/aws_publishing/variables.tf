variable "region" {
  type    = string
  default = "eu-central-1"
}
variable "name" {
  type    = string
  default = "aws-mesouq"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,35}$", var.name))
    error_message = "Use a lowercase resource prefix of 3–36 characters."
  }
}
variable "github_repository" {
  type        = string
  default     = "BrieucB/AWS-MesoUQ"
  description = "Exact owner/repository allowed to publish image/static assets through OIDC."
}
variable "github_environment" {
  type    = string
  default = "aws-publishing"
}
variable "github_oidc_subject_prefix" {
  type        = string
  description = "Exact sub_claim_prefix reported by the repository OIDC settings API, including immutable owner/repository IDs when enabled. Supply privately; no environment/ref suffix."
  validation {
    condition     = can(regex("^repo:[A-Za-z0-9_.-]+(@[0-9]+)?/[A-Za-z0-9_.-]+(@[0-9]+)?$", var.github_oidc_subject_prefix))
    error_message = "Use the exact repo:OWNER/REPOSITORY prefix, including @ID suffixes when configured. Wildcards and environment/ref suffixes are forbidden."
  }
}
variable "github_oidc_provider_arn" {
  type        = string
  description = "Existing GitHub OIDC provider ARN in the target account; avoids duplicate providers."
}
variable "image_uri" {
  type        = string
  description = "Prebuilt ECR container image pinned by sha256 digest. Bootstrap ECR first."
  validation {
    condition     = can(regex("@sha256:[a-f0-9]{64}$", var.image_uri))
    error_message = "Lambda images must be immutable digest references."
  }
}
variable "memberships" {
  sensitive   = true
  description = "Operator-approved Cognito subject to email/project-role mappings, maximum five. No credentials."
  type        = map(object({ email = string, projects = map(string), connector_public_keys = optional(map(string), {}) }))
  default     = {}
  validation {
    condition     = length(var.memberships) <= 5 && alltrue(flatten([for member in values(var.memberships) : [for role in values(member.projects) : contains(["member", "maintainer"], role)]]))
    error_message = "At most five invited subjects with member/maintainer project roles."
  }
  validation {
    condition     = alltrue(flatten([for member in values(var.memberships) : [for key in values(member.connector_public_keys) : can(regex("^[A-Za-z0-9+/]{43}=$", key))]]))
    error_message = "Connector keys must encode raw 32-byte Ed25519 public keys as base64. Private keys never enter Terraform."
  }
}
variable "projects" {
  type    = map(object({ name = string }))
  default = {}
}
variable "trusted_models" {
  type        = map(object({ project_id = string, version = string, sha256 = map(string), format = string, status = string, artifact_id = string, validation_artifact_id = string, provenance_json = optional(string), approved_adapter_sha256 = string }))
  default     = {}
  description = "Operator-only immutable canonical registry; v1 requires the approved uq_emb_sonovue_202607 pipeline_binding in portable provenance_json. Private scientific_profile/artifact_root stays on HPC."
  validation {
    condition     = alltrue([for m in values(var.trusted_models) : m.provenance_json == null ? true : can(keys(jsondecode(m.provenance_json)))])
    error_message = "provenance_json must encode a JSON object."
  }
  validation {
    condition     = alltrue([for m in values(var.trusted_models) : contains(["safetensors", "onnx", "mesouq_checkpoint"], m.format) && length(m.sha256) > 0 && alltrue([for diameter in keys(m.sha256) : contains(["3.2", "3.4", "5.8"], diameter)]) && alltrue([for sha in values(m.sha256) : can(regex("^[a-f0-9]{64}$", sha))]) && m.status == "released"])
    error_message = "Registry entries require an approved trusted model format, checksum bundle and released bootstrap status."
  }
  validation {
    condition     = alltrue([for m in values(var.trusted_models) : can(regex("^[a-f0-9]{64}$", m.approved_adapter_sha256))])
    error_message = "Release v1 requires the operator-approved public adapter source SHA256."
  }
}
variable "budget_usd" {
  type        = string
  default     = "0.01"
  description = "Monitoring-only near-zero budget; this does not enforce a hard spend cap or change the AWS plan."
}

variable "connector_callback_url" {
  type        = string
  default     = "http://localhost:8765/callback"
  description = "Exact connector PKCE callback. AWS documents HTTP loopback callbacks as testing-only; production HTTPS requires an owner-reviewed callback integration."
  validation {
    condition     = can(regex("^https://[^#]+$", var.connector_callback_url)) || can(regex("^http://(127\\.0\\.0\\.1|localhost):[0-9]+/callback$", var.connector_callback_url))
    error_message = "Use an exact HTTPS callback or explicitly registered HTTP loopback testing callback."
  }
}

variable "site_signing_public_key_pem" {
  type        = string
  description = "Owner-generated RSA2048 public key only. Matching private PKCS8 PEM must be provisioned separately in SSM Standard SecureString; never pass private material to Terraform."
  validation {
    condition     = startswith(var.site_signing_public_key_pem, "-----BEGIN PUBLIC KEY-----")
    error_message = "Supply a PEM public key, never a private key."
  }
}
variable "site_signing_key_parameter_name" {
  type        = string
  description = "Existing owner-provisioned Standard SecureString parameter containing matching RSA2048 private PEM, encrypted with default aws/ssm managed key."
  validation {
    condition     = can(regex("^/[A-Za-z0-9_./-]+$", var.site_signing_key_parameter_name))
    error_message = "Use an absolute SSM parameter name."
  }
}
