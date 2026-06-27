-- Seed a representative sample knowledge graph into Apache AGE so the
-- graph console (AGE Viewer) and Adminer Cypher have real data to show.
-- Idempotent: drops + recreates the `knowledge_graph` graph.
--
--   docker exec -i caliber-mcp-postgres psql -U caliber -d caliber < deploy/age-viewer/seed-graph.sql
--
-- Then in AGE Viewer / Adminer, query e.g.:
--   SELECT * FROM cypher('knowledge_graph', $$ MATCH (n)-[r]->(m) RETURN n,r,m $$) AS (n agtype, r agtype, m agtype);

LOAD 'age';
SET search_path = ag_catalog, "$user", public;

SELECT drop_graph('knowledge_graph', true) FROM ag_graph WHERE name = 'knowledge_graph';
SELECT create_graph('knowledge_graph');

-- Entities + relationships + business rules, with edges between them.
SELECT * FROM cypher('knowledge_graph', $$
  CREATE
    (borrower:Entity   {name:'Borrower'}),
    (property:Entity   {name:'Property'}),
    (loan:Entity       {name:'Loan'}),
    (lender:Entity     {name:'Lender'}),
    (income:Entity     {name:'Income'}),
    (credit:Entity     {name:'CreditScore'}),
    (appraisal:Entity  {name:'Appraisal'}),
    (doc:Entity        {name:'Document'}),

    (borrower)-[:APPLIES_FOR]->(loan),
    (lender)-[:ISSUES]->(loan),
    (loan)-[:SECURED_BY]->(property),
    (property)-[:HAS]->(appraisal),
    (borrower)-[:HAS]->(income),
    (borrower)-[:HAS]->(credit),
    (loan)-[:REQUIRES]->(doc),

    (r1:Rule {rule_id:'R-001', name:'DTI must be below 43%',        rule_type:'eligibility'}),
    (r2:Rule {rule_id:'R-002', name:'Minimum credit score 620',     rule_type:'eligibility'}),
    (r3:Rule {rule_id:'R-003', name:'LTV must not exceed 80%',      rule_type:'risk'}),
    (r4:Rule {rule_id:'R-004', name:'Appraisal required before close', rule_type:'process'}),
    (r5:Rule {rule_id:'R-005', name:'Income docs retained 7 years', rule_type:'compliance'}),

    (r1)-[:APPLIES_TO]->(income),
    (r2)-[:APPLIES_TO]->(credit),
    (r3)-[:APPLIES_TO]->(loan),
    (r3)-[:APPLIES_TO]->(property),
    (r4)-[:APPLIES_TO]->(appraisal),
    (r5)-[:APPLIES_TO]->(doc)
$$) AS (v agtype);

-- Quick counts so the load is self-verifying in the psql output.
SELECT 'nodes' AS kind, count(*) FROM cypher('knowledge_graph', $$ MATCH (n) RETURN n $$) AS (n agtype)
UNION ALL
SELECT 'edges', count(*) FROM cypher('knowledge_graph', $$ MATCH ()-[r]->() RETURN r $$) AS (r agtype);
