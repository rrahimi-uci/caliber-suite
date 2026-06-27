# Information Security Policy

Source id: SECURITY-POLICY
Policy domain: security
Owner: Security & Trust
Last reviewed: 2026-02-15

## Authentication

All employee and customer-admin accounts must use multi-factor authentication
(MFA). Password-only access is not permitted for any account that can change
billing, manage users, or access customer data. Single sign-on (SSO) via SAML or
OIDC is available on Enterprise plans and, when enabled, MFA is enforced by the
identity provider.

## Encryption

Customer data is encrypted in transit using TLS 1.2 or higher. Data at rest is
encrypted using AES-256. Encryption keys are managed in a dedicated key
management service and rotated at least every 12 months.

## Access control

Access to production systems follows least privilege and is granted on a
need-to-know basis. Production access requires an approved access request and is
reviewed quarterly. All production access is logged.

## Incident response

Suspected security incidents must be reported to the Security & Trust team
immediately. Confirmed incidents follow a documented response process:
containment, eradication, recovery, and post-incident review. Affected customers
are notified in line with the contractual and regulatory timelines in their
agreement.

## Vendor and subprocessor security

Third-party subprocessors are reviewed before onboarding and must meet our
security and data-protection requirements. The current subprocessor list is
maintained by Security & Trust and available on request.
