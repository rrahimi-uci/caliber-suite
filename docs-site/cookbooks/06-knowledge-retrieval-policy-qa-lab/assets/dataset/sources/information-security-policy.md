# Information Security Policy

Document ID: POL-SEC-001
Policy domain: security
Owner: Security Engineering
Version: 5.0
Effective date: 2026-01-15

## 1. Purpose and scope

This policy defines the minimum security controls for every system that stores or
processes CALIBER or customer information, and for every employee, contractor and
service account that uses those systems. Where a team policy is stricter, the stricter
rule applies.

## 2. Authentication

Multi-factor authentication (MFA) is required for every workforce account. MFA is also
required for every customer account that holds an administrator role, which includes
any account that can change billing details or access customer data. Password-only
access is not permitted for those accounts.

Passwords must be at least 14 characters long. Passwords are not rotated on a schedule;
they must be changed immediately when compromise is suspected. Shared accounts are
prohibited. Service accounts use rotating keys, never passwords.

## 3. Encryption

Customer data at rest is encrypted with AES-256. Customer data in transit is protected
with TLS 1.2 or higher; TLS 1.3 is preferred and is the default on public endpoints.
Encryption keys are held in the managed key service and rotated every 12 months.
Laptops use full-disk encryption, enforced by device management.

## 4. Access control

Access follows least privilege. Production access is granted just in time, for at most
8 hours, and every grant is logged. Access to systems that hold customer data is
reviewed every quarter by the system owner, and unused access is removed. Access is
revoked by the end of an employee's last working day.

## 5. Vulnerability management

Findings are rated by severity and must be fixed within these limits, counted from the
date the finding is confirmed:

| Severity | Remediation deadline |
| --- | --- |
| Critical | 7 days |
| High | 30 days |
| Medium | 90 days |
| Low | 180 days |

A finding that cannot be fixed in time needs a documented exception signed by the
Director of Security Engineering, with a compensating control and an expiry date.
External penetration tests are run once a year by an independent firm.

## 6. Security incidents

Anyone who suspects a security incident must report it to security@caliber.example within
**24 hours** of becoming aware of it. Do not wait to confirm the suspicion. The Security
on-call engineer acknowledges a report within 1 hour and assigns a severity.

For a confirmed incident that affects customer data, the Security team notifies affected
customers within **72 hours** of confirmation, and notifies regulators where the law
requires it. A written post-incident review is completed within 10 business days.

## 7. Third parties

Vendors that process customer data must pass a security review before use and sign a data
processing agreement. See the Vendor and Procurement Policy for the review process.

## 8. Exceptions and enforcement

Exceptions to this policy need written approval from the Director of Security
Engineering and expire after at most 12 months. Violations can lead to loss of access and
disciplinary action.
