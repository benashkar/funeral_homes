USE funeral_homes;

-- Normalized funeral homes table with address and geocoding fields.
-- legacy_fh_id (e.g. "fh-1234") is extracted from BreadcrumbList JSON-LD URLs.
CREATE TABLE IF NOT EXISTS funeral_homes (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    legacy_fh_id    VARCHAR(20)   NOT NULL UNIQUE,
    name            VARCHAR(255)  NOT NULL,
    address         VARCHAR(500)  DEFAULT NULL,
    city            VARCHAR(100)  DEFAULT NULL,
    state           VARCHAR(50)   DEFAULT NULL,
    zip             VARCHAR(10)   DEFAULT NULL,
    lat             DECIMAL(10,7) DEFAULT NULL,
    lon             DECIMAL(10,7) DEFAULT NULL,
    legacy_url      VARCHAR(500)  DEFAULT NULL,
    created_at      DATETIME      DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME      DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_city_state (city, state),
    INDEX idx_name (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Add FK column to obituaries (nullable — backfilled by scripts/backfill_funeral_homes.py)
ALTER TABLE obituaries
    ADD COLUMN funeral_home_id INT DEFAULT NULL AFTER funeral_home,
    ADD INDEX idx_funeral_home_id (funeral_home_id),
    ADD CONSTRAINT fk_obit_funeral_home
        FOREIGN KEY (funeral_home_id) REFERENCES funeral_homes(id)
        ON DELETE SET NULL;
