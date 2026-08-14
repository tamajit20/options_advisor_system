-- Optional one-time fix: align next SIG # with MAX(id) (does not renumber existing rows).
-- Run on VM after deploy if identity cache created large gaps:
--
--   docker compose exec -T sqlserver /opt/mssql-tools18/bin/sqlcmd \
--     -S localhost -U sa -P "$MSSQL_SA_PASSWORD" -C -d OptionsAdvisorDB \
--     -i /path/to/reseed-scout-identities.sql
--
-- Forward-only: new signals get MAX(id)+1, MAX(id)+2, ... (no 1000-jumps once TF 272 is on).

SET NOCOUNT ON;

DECLARE @max_sig BIGINT = (SELECT ISNULL(MAX(id), 0) FROM scout_signals);
DECLARE @max_trd BIGINT = (SELECT ISNULL(MAX(id), 0) FROM scout_trades);

DBCC CHECKIDENT ('scout_signals', RESEED, @max_sig);
DBCC CHECKIDENT ('scout_trades', RESEED, @max_trd);

SELECT 'scout_signals' AS tbl, @max_sig AS reseeded_to, IDENT_CURRENT('scout_signals') AS next_identity;
SELECT 'scout_trades' AS tbl, @max_trd AS reseeded_to, IDENT_CURRENT('scout_trades') AS next_identity;
