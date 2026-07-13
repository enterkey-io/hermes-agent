# Selected Fact Policy

Selected-fact capture is not part of this rollout. The broker has no capture adapter and returns `forbidden` with zero GBrain calls. Do not construct or submit a fact, slug, frontmatter, source ID, file path, or arbitrary content.

Any future capture rollout requires a separately approved CLI write contract, validator/renderer review, transactional multi-writer testing, production rollout authorization, and updated broker/client code. Read-only installation does not grant that authority.
