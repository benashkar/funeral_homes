USE funeral_homes;

-- Add s3_photo_url column for S3-hosted photo copies.
-- Original photo_url (Legacy CDN) is preserved; s3_photo_url is our own copy.
ALTER TABLE obituaries
    ADD COLUMN s3_photo_url VARCHAR(500) DEFAULT NULL AFTER photo_url;
