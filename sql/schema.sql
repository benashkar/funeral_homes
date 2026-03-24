CREATE DATABASE IF NOT EXISTS funeral_homes
    CHARACTER SET utf8mb4
    COLLATE utf8mb4_unicode_ci;

USE funeral_homes;

CREATE TABLE IF NOT EXISTS obituaries (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    site_id         VARCHAR(50)   NOT NULL,
    legacy_url      VARCHAR(500)  NOT NULL UNIQUE,   -- dedup key
    deceased_name   VARCHAR(255),
    published_date  DATE,
    death_date      DATE,
    death_city      VARCHAR(100)  DEFAULT NULL,
    death_state     VARCHAR(10)   DEFAULT NULL,
    funeral_home    VARCHAR(255),
    photo_url       VARCHAR(500)  DEFAULT NULL,
    obit_text       LONGTEXT,
    scraped_at      DATETIME      DEFAULT CURRENT_TIMESTAMP,
    sent_to_cms     TINYINT(1)    DEFAULT 0,
    is_deleted      TINYINT(1)    DEFAULT 0,
    INDEX idx_site_id (site_id),
    INDEX idx_published_date (published_date),
    INDEX idx_death_city (death_city),
    INDEX idx_sent_to_cms (sent_to_cms),
    INDEX idx_is_deleted (is_deleted)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS scrape_log (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    site_id     VARCHAR(50) NOT NULL,
    run_date    DATE        NOT NULL,
    obits_found INT         DEFAULT 0,
    obits_new   INT         DEFAULT 0,
    errors      TEXT,
    run_at      DATETIME    DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_run_date (run_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
