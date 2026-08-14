--
-- PostgreSQL database cluster dump
--

\restrict ZAoFYNVwadWSoevAyH0x56prbaeROxea9wlGqhniPbcXMs4CVMFvqncAZ0RpeWn

SET default_transaction_read_only = off;

SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;

--
-- Roles
--

CREATE ROLE gr_user;
ALTER ROLE gr_user WITH SUPERUSER INHERIT CREATEROLE CREATEDB LOGIN REPLICATION BYPASSRLS PASSWORD 'SCRAM-SHA-256$4096:PgYIUtEL0QsQoGQPtiVgTQ==$X7TaC/ks7daqQAKBb04cCsUkssSS5kDRT67KJXefymw=:XZuBa7KTUaHY+DqSVUTmqw5YB4P3xiZJggTaBDce/nM=';

--
-- User Configurations
--








\unrestrict ZAoFYNVwadWSoevAyH0x56prbaeROxea9wlGqhniPbcXMs4CVMFvqncAZ0RpeWn

--
-- PostgreSQL database cluster dump complete
--

