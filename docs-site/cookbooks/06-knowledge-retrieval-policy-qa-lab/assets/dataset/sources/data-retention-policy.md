# Data Retention and Deletion Policy

Document ID: POL-SEC-007
Policy domain: security
Owner: Security Engineering and Legal
Version: 2.4
Effective date: 2025-07-01

## 1. Purpose

This policy states how long CALIBER keeps each kind of data and how it is deleted when
the period ends. Keeping data longer than necessary increases risk; deleting it too early
can breach legal or contractual duties. The periods below balance the two.

## 2. Retention schedule

| Data category | Retention period | Starts when | Deletion method |
| --- | --- | --- | --- |
| Security audit logs | 365 days | The event is recorded | Automatic purge |
| Application logs | 90 days | The event is recorded | Automatic purge |
| Authentication (access) logs | 180 days | The event is recorded | Automatic purge |
| Encrypted backups | 35 days, rolling | The backup is taken | Overwritten by newer backups |
| Customer content | Deleted within 30 days | The account is closed | Cryptographic erasure plus storage deletion |
| Billing and invoice records | 7 years | The invoice date | Manual review, then deletion |
| Support tickets | 3 years | The ticket is closed | Automatic purge |
| Employee HR records | 7 years | Employment ends | Manual review, then deletion |

The retention period of security audit logs is longer than that of application logs
because audit logs support incident investigations and customer audits that can look
back a full year.

## 3. Customer content after closure

When an account is closed, the customer's content is deleted within 30 days. During that
time the customer can ask Support to reopen the account. After deletion the content
cannot be recovered, because backups containing it roll off after 35 days. On request,
Support sends a deletion confirmation within 10 business days of the deletion.

## 4. Legal holds

Legal may place a legal hold on data that is relevant to litigation, an investigation or
a regulatory request. A legal hold suspends every deletion in this policy for the data it
covers, including the automatic purges, until Legal lifts the hold in writing. Employees
must not delete data that is under a legal hold, even if its retention period has ended.

## 5. Backups

Backups are encrypted and kept on a rolling 35-day basis. A restore from backup is only
used for disaster recovery and never to retrieve data that a customer asked to be
deleted. If a deletion request is made while a backup that contains the data exists, the
data leaves the backup when the backup is overwritten.

## 6. Exceptions

A system owner who needs a different retention period must submit a request to Security
Engineering and Legal. Approved exceptions are recorded in the retention register and are
reviewed every year.
