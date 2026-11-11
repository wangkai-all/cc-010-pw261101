-- 05_cycle.sql：对象间循环依赖，必须指出循环路径而不是无限递归

CREATE VIEW cyc_a AS SELECT id FROM cyc_b;

CREATE VIEW cyc_b AS SELECT id FROM cyc_a;
