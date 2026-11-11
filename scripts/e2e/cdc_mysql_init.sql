-- Deterministic CDC canary fixture: replication grants for the public
-- fixture account. Applied once on first container initialization only.
-- Debezium needs streaming rights plus RELOAD for the schema-only initial
-- snapshot lock. The image's MYSQL_USER also has database-local fixture DML
-- rights for the acceptance scenarios; this is not a least-privilege account.
-- MySQL 8.4 GRANT syntax keeps the REPLICATION SLAVE spelling (also the
-- exact marker the platform CDC preflight matches in SHOW GRANTS).
GRANT REPLICATION SLAVE, REPLICATION CLIENT, SHOW DATABASES, RELOAD ON *.* TO 'cdc_e2e'@'%';
GRANT SELECT ON cdc_source.* TO 'cdc_e2e'@'%';
FLUSH PRIVILEGES;
