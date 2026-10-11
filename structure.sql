-- MySQL dump 10.13  Distrib 8.0.25-15, for Linux (x86_64)
--
-- Host: localhost    Database: u3381989_default
-- ------------------------------------------------------
-- Server version	8.0.25-15

/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;
/*!40101 SET @OLD_CHARACTER_SET_RESULTS=@@CHARACTER_SET_RESULTS */;
/*!40101 SET @OLD_COLLATION_CONNECTION=@@COLLATION_CONNECTION */;
/*!50503 SET NAMES utf8mb4 */;
/*!40103 SET @OLD_TIME_ZONE=@@TIME_ZONE */;
/*!40103 SET TIME_ZONE='+00:00' */;
/*!40014 SET @OLD_UNIQUE_CHECKS=@@UNIQUE_CHECKS, UNIQUE_CHECKS=0 */;
/*!40014 SET @OLD_FOREIGN_KEY_CHECKS=@@FOREIGN_KEY_CHECKS, FOREIGN_KEY_CHECKS=0 */;
/*!40101 SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO' */;
/*!40111 SET @OLD_SQL_NOTES=@@SQL_NOTES, SQL_NOTES=0 */;
/*!50717 SELECT COUNT(*) INTO @rocksdb_has_p_s_session_variables FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = 'performance_schema' AND TABLE_NAME = 'session_variables' */;
/*!50717 SET @rocksdb_get_is_supported = IF (@rocksdb_has_p_s_session_variables, 'SELECT COUNT(*) INTO @rocksdb_is_supported FROM performance_schema.session_variables WHERE VARIABLE_NAME=\'rocksdb_bulk_load\'', 'SELECT 0') */;
/*!50717 PREPARE s FROM @rocksdb_get_is_supported */;
/*!50717 EXECUTE s */;
/*!50717 DEALLOCATE PREPARE s */;
/*!50717 SET @rocksdb_enable_bulk_load = IF (@rocksdb_is_supported, 'SET SESSION rocksdb_bulk_load = 1', 'SET @rocksdb_dummy_bulk_load = 0') */;
/*!50717 PREPARE s FROM @rocksdb_enable_bulk_load */;
/*!50717 EXECUTE s */;
/*!50717 DEALLOCATE PREPARE s */;

--
-- Table structure for table `abonents`
--

DROP TABLE IF EXISTS `abonents`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `abonents` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `type` enum('user','specialist') NOT NULL,
  `user_id` int DEFAULT NULL,
  `specialist_id` bigint DEFAULT NULL,
  `phone_e164` varchar(20) NOT NULL,
  `phone_verified` tinyint(1) NOT NULL DEFAULT '0',
  `is_active` tinyint(1) NOT NULL DEFAULT '1',
  `can_make_calls` tinyint(1) NOT NULL DEFAULT '1',
  `can_receive_calls` tinyint(1) NOT NULL DEFAULT '1',
  `preferred_channel` varchar(20) NOT NULL DEFAULT 'pstn',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `phone` varchar(32) DEFAULT NULL,
  `display_name` varchar(128) DEFAULT NULL,
  `entity_type` enum('client','specialist') DEFAULT NULL,
  `client_user_id` bigint DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_abonents_phone_e164` (`phone_e164`),
  UNIQUE KEY `uq_abonents_user_id` (`user_id`),
  UNIQUE KEY `uq_abonents_specialist_id` (`specialist_id`),
  KEY `idx_abonents_type_active` (`type`,`is_active`),
  KEY `idx_abonents_phone_verified` (`phone_verified`),
  KEY `ix_abonents_phone_e164` (`phone_e164`),
  CONSTRAINT `fk_abonents_specialist` FOREIGN KEY (`specialist_id`) REFERENCES `specialists` (`id`) ON DELETE CASCADE ON UPDATE CASCADE,
  CONSTRAINT `fk_abonents_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=337 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!50003 SET @saved_cs_client      = @@character_set_client */ ;
/*!50003 SET @saved_cs_results     = @@character_set_results */ ;
/*!50003 SET @saved_col_connection = @@collation_connection */ ;
/*!50003 SET character_set_client  = utf8mb4 */ ;
/*!50003 SET character_set_results = utf8mb4 */ ;
/*!50003 SET collation_connection  = utf8mb4_0900_ai_ci */ ;
/*!50003 SET @saved_sql_mode       = @@sql_mode */ ;
/*!50003 SET sql_mode              = 'NO_ENGINE_SUBSTITUTION' */ ;
DELIMITER ;;
/*!50003 CREATE*/ /*!50017 DEFINER=`u3381989_default`@`localhost`*/ /*!50003 TRIGGER `trg_abonents_check_owner_insert` BEFORE INSERT ON `abonents` FOR EACH ROW BEGIN
    IF NOT (
        (NEW.`type` = 'user' AND NEW.`user_id` IS NOT NULL AND NEW.`specialist_id` IS NULL)
        OR
        (NEW.`type` = 'specialist' AND NEW.`specialist_id` IS NOT NULL AND NEW.`user_id` IS NULL)
    ) THEN
        SIGNAL SQLSTATE '45000'
        SET MESSAGE_TEXT = 'Invalid owner: user must have user_id, specialist must have specialist_id';
    END IF;
END */;;
DELIMITER ;
/*!50003 SET sql_mode              = @saved_sql_mode */ ;
/*!50003 SET character_set_client  = @saved_cs_client */ ;
/*!50003 SET character_set_results = @saved_cs_results */ ;
/*!50003 SET collation_connection  = @saved_col_connection */ ;
/*!50003 SET @saved_cs_client      = @@character_set_client */ ;
/*!50003 SET @saved_cs_results     = @@character_set_results */ ;
/*!50003 SET @saved_col_connection = @@collation_connection */ ;
/*!50003 SET character_set_client  = utf8mb4 */ ;
/*!50003 SET character_set_results = utf8mb4 */ ;
/*!50003 SET collation_connection  = utf8mb4_0900_ai_ci */ ;
/*!50003 SET @saved_sql_mode       = @@sql_mode */ ;
/*!50003 SET sql_mode              = 'NO_ENGINE_SUBSTITUTION' */ ;
DELIMITER ;;
/*!50003 CREATE*/ /*!50017 DEFINER=`u3381989_default`@`localhost`*/ /*!50003 TRIGGER `trg_abonents_check_owner_update` BEFORE UPDATE ON `abonents` FOR EACH ROW BEGIN
    IF NOT (
        (NEW.`type` = 'user' AND NEW.`user_id` IS NOT NULL AND NEW.`specialist_id` IS NULL)
        OR
        (NEW.`type` = 'specialist' AND NEW.`specialist_id` IS NOT NULL AND NEW.`user_id` IS NULL)
    ) THEN
        SIGNAL SQLSTATE '45000'
        SET MESSAGE_TEXT = 'Invalid owner: user must have user_id, specialist must have specialist_id';
    END IF;
END */;;
DELIMITER ;
/*!50003 SET sql_mode              = @saved_sql_mode */ ;
/*!50003 SET character_set_client  = @saved_cs_client */ ;
/*!50003 SET character_set_results = @saved_cs_results */ ;
/*!50003 SET collation_connection  = @saved_col_connection */ ;

--
-- Table structure for table `alfabank_ip_applications`
--

DROP TABLE IF EXISTS `alfabank_ip_applications`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `alfabank_ip_applications` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL,
  `tg_user_id` bigint DEFAULT NULL,
  `alfabank_id` varchar(128) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `status` enum('pending','approved','declined') COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'pending',
  `alfabank_state` varchar(64) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `alfabank_raw` text COLLATE utf8mb4_unicode_ci,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_alfabank_ip_alfabank_id` (`alfabank_id`),
  KEY `idx_alfabank_ip_user` (`user_id`),
  KEY `idx_alfabank_ip_tg` (`tg_user_id`),
  KEY `idx_alfabank_ip_status` (`status`),
  CONSTRAINT `fk_alfabank_ip_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=4 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `auth_group`
--

DROP TABLE IF EXISTS `auth_group`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `auth_group` (
  `id` int NOT NULL AUTO_INCREMENT,
  `name` varchar(150) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `name` (`name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `auth_group_permissions`
--

DROP TABLE IF EXISTS `auth_group_permissions`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `auth_group_permissions` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `group_id` int NOT NULL,
  `permission_id` int NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `auth_group_permissions_group_id_permission_id_0cd325b0_uniq` (`group_id`,`permission_id`),
  KEY `auth_group_permissio_permission_id_84c5c92e_fk_auth_perm` (`permission_id`),
  CONSTRAINT `auth_group_permissio_permission_id_84c5c92e_fk_auth_perm` FOREIGN KEY (`permission_id`) REFERENCES `auth_permission` (`id`),
  CONSTRAINT `auth_group_permissions_group_id_b120cbf9_fk_auth_group_id` FOREIGN KEY (`group_id`) REFERENCES `auth_group` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `auth_permission`
--

DROP TABLE IF EXISTS `auth_permission`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `auth_permission` (
  `id` int NOT NULL AUTO_INCREMENT,
  `name` varchar(255) NOT NULL,
  `content_type_id` int NOT NULL,
  `codename` varchar(100) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `auth_permission_content_type_id_codename_01ab375a_uniq` (`content_type_id`,`codename`),
  CONSTRAINT `auth_permission_content_type_id_2f476e4b_fk_django_co` FOREIGN KEY (`content_type_id`) REFERENCES `django_content_type` (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=237 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `auth_user`
--

DROP TABLE IF EXISTS `auth_user`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `auth_user` (
  `id` int NOT NULL AUTO_INCREMENT,
  `password` varchar(128) NOT NULL,
  `last_login` datetime(6) DEFAULT NULL,
  `is_superuser` tinyint(1) NOT NULL,
  `username` varchar(150) NOT NULL,
  `first_name` varchar(150) NOT NULL,
  `last_name` varchar(150) NOT NULL,
  `email` varchar(254) NOT NULL,
  `is_staff` tinyint(1) NOT NULL,
  `is_active` tinyint(1) NOT NULL,
  `date_joined` datetime(6) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `username` (`username`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `auth_user_groups`
--

DROP TABLE IF EXISTS `auth_user_groups`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `auth_user_groups` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL,
  `group_id` int NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `auth_user_groups_user_id_group_id_94350c0c_uniq` (`user_id`,`group_id`),
  KEY `auth_user_groups_group_id_97559544_fk_auth_group_id` (`group_id`),
  CONSTRAINT `auth_user_groups_group_id_97559544_fk_auth_group_id` FOREIGN KEY (`group_id`) REFERENCES `auth_group` (`id`),
  CONSTRAINT `auth_user_groups_user_id_6a12ed8b_fk_auth_user_id` FOREIGN KEY (`user_id`) REFERENCES `auth_user` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `auth_user_user_permissions`
--

DROP TABLE IF EXISTS `auth_user_user_permissions`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `auth_user_user_permissions` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL,
  `permission_id` int NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `auth_user_user_permissions_user_id_permission_id_14a6b632_uniq` (`user_id`,`permission_id`),
  KEY `auth_user_user_permi_permission_id_1fbb5f2c_fk_auth_perm` (`permission_id`),
  CONSTRAINT `auth_user_user_permi_permission_id_1fbb5f2c_fk_auth_perm` FOREIGN KEY (`permission_id`) REFERENCES `auth_permission` (`id`),
  CONSTRAINT `auth_user_user_permissions_user_id_a95ead1b_fk_auth_user_id` FOREIGN KEY (`user_id`) REFERENCES `auth_user` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `bonus_product_policies`
--

DROP TABLE IF EXISTS `bonus_product_policies`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `bonus_product_policies` (
  `id` int NOT NULL AUTO_INCREMENT,
  `product_type` varchar(64) NOT NULL COMMENT 'order, service, ofd, edo, kkt и т.д.',
  `event_type` varchar(64) NOT NULL COMMENT 'purchase, first_purchase, repeat_purchase',
  `points_fixed` int DEFAULT NULL COMMENT 'Фиксированное кол-во баллов (NULL = не используется)',
  `points_per_rub` decimal(8,4) DEFAULT NULL COMMENT 'Баллов за 1 рубль (NULL = не используется)',
  `points_lifetime_days` int DEFAULT NULL COMMENT 'Срок жизни баллов (NULL = system default)',
  `min_order_rub` int DEFAULT NULL COMMENT 'Мин. сумма заказа для начисления',
  `max_points` int DEFAULT NULL COMMENT 'Потолок начисления за одну операцию',
  `is_active` tinyint(1) NOT NULL DEFAULT '1',
  `priority` int NOT NULL DEFAULT '0' COMMENT 'Приоритет: большее значение = выше',
  `valid_from` datetime DEFAULT NULL,
  `valid_until` datetime DEFAULT NULL,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `ix_bpp_lookup` (`product_type`,`event_type`,`is_active`),
  KEY `ix_bpp_valid` (`valid_from`,`valid_until`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `bot_events`
--

DROP TABLE IF EXISTS `bot_events`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `bot_events` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `ts` timestamp(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `tg_user_id` bigint DEFAULT NULL,
  `chat_id` bigint DEFAULT NULL,
  `message_id` bigint DEFAULT NULL,
  `update_id` bigint DEFAULT NULL,
  `event_type` varchar(32) NOT NULL,
  `name` varchar(128) DEFAULT NULL,
  `callback_data` text,
  `text_len` int DEFAULT NULL,
  `has_file` tinyint(1) DEFAULT NULL,
  `payload` json DEFAULT NULL,
  `user_id` int DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_ts` (`ts`),
  KEY `idx_user_ts` (`tg_user_id`,`ts`),
  KEY `idx_type_name_ts` (`event_type`,`name`,`ts`),
  KEY `fk_events_users` (`user_id`),
  CONSTRAINT `fk_events_users` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=16261 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `bot_events_weekly`
--

DROP TABLE IF EXISTS `bot_events_weekly`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `bot_events_weekly` (
  `week_start` date NOT NULL,
  `event_type` varchar(32) NOT NULL,
  `name` varchar(128) NOT NULL DEFAULT '',
  `cnt_total` bigint NOT NULL,
  `cnt_with_file` bigint NOT NULL,
  `avg_text_len` double DEFAULT NULL,
  `uniq_users` bigint NOT NULL,
  `first_ts` timestamp(3) NULL DEFAULT NULL,
  `last_ts` timestamp(3) NULL DEFAULT NULL,
  PRIMARY KEY (`week_start`,`event_type`,`name`),
  KEY `idx_week` (`week_start`),
  KEY `idx_type` (`event_type`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `calendar_event_categories`
--

DROP TABLE IF EXISTS `calendar_event_categories`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `calendar_event_categories` (
  `id` int NOT NULL AUTO_INCREMENT,
  `code` varchar(50) NOT NULL,
  `name` varchar(100) NOT NULL,
  `color` varchar(20) NOT NULL DEFAULT '#667eea',
  `icon` varchar(10) DEFAULT '',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `code` (`code`)
) ENGINE=InnoDB AUTO_INCREMENT=6 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `calendar_events`
--

DROP TABLE IF EXISTS `calendar_events`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `calendar_events` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `event_date` date NOT NULL,
  `event_time` time DEFAULT NULL,
  `title` varchar(255) NOT NULL,
  `description` text,
  `category_id` int DEFAULT NULL,
  `client_id` int DEFAULT NULL,
  `created_by` bigint NOT NULL,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_event_date` (`event_date`),
  KEY `idx_category` (`category_id`),
  KEY `idx_client` (`client_id`),
  KEY `idx_created_by` (`created_by`),
  CONSTRAINT `fk_event_category` FOREIGN KEY (`category_id`) REFERENCES `calendar_event_categories` (`id`) ON DELETE SET NULL,
  CONSTRAINT `fk_event_client` FOREIGN KEY (`client_id`) REFERENCES `users` (`id`) ON DELETE SET NULL,
  CONSTRAINT `fk_event_created_by` FOREIGN KEY (`created_by`) REFERENCES `specialists` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=2 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `call_logs`
--

DROP TABLE IF EXISTS `call_logs`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `call_logs` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `call_session_id` bigint NOT NULL,
  `event_type` varchar(50) NOT NULL,
  `event_payload` json DEFAULT NULL,
  `event_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `raw_callerid` varchar(50) DEFAULT NULL,
  `trunk_name` varchar(100) DEFAULT NULL,
  `asterisk_channel` varchar(255) DEFAULT NULL,
  `hangup_cause` varchar(100) DEFAULT NULL,
  `duration_sec` int DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_call_logs_session_time` (`call_session_id`,`event_time`),
  KEY `idx_call_logs_event_type` (`event_type`),
  KEY `idx_call_logs_event_time` (`event_time`),
  CONSTRAINT `fk_call_logs_session` FOREIGN KEY (`call_session_id`) REFERENCES `call_sessions` (`id`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `call_sessions`
--

DROP TABLE IF EXISTS `call_sessions`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `call_sessions` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `initiator_type` enum('user','specialist','system') NOT NULL,
  `initiator_abonent_id` bigint NOT NULL,
  `caller_abonent_id` bigint NOT NULL,
  `callee_abonent_id` bigint NOT NULL,
  `direction` enum('user_to_specialist','specialist_to_user') NOT NULL,
  `channel` enum('pstn') NOT NULL DEFAULT 'pstn',
  `status` enum('created','validated','dialing_first_leg','first_leg_answered','dialing_second_leg','bridged','completed','failed','cancelled','expired') NOT NULL DEFAULT 'created',
  `asterisk_linkedid` varchar(64) DEFAULT NULL,
  `asterisk_first_channel` varchar(255) DEFAULT NULL,
  `asterisk_second_channel` varchar(255) DEFAULT NULL,
  `started_at` datetime DEFAULT NULL,
  `bridged_at` datetime DEFAULT NULL,
  `ended_at` datetime DEFAULT NULL,
  `expires_at` datetime DEFAULT NULL,
  `failure_reason` varchar(255) DEFAULT NULL,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_call_sessions_status` (`status`),
  KEY `idx_call_sessions_created_at` (`created_at`),
  KEY `idx_call_sessions_direction_status` (`direction`,`status`),
  KEY `idx_call_sessions_initiator` (`initiator_abonent_id`,`created_at`),
  KEY `idx_call_sessions_pair` (`caller_abonent_id`,`callee_abonent_id`,`created_at`),
  KEY `idx_call_sessions_linkedid` (`asterisk_linkedid`),
  KEY `fk_call_sessions_callee_abonent` (`callee_abonent_id`),
  CONSTRAINT `fk_call_sessions_callee_abonent` FOREIGN KEY (`callee_abonent_id`) REFERENCES `abonents` (`id`) ON DELETE RESTRICT ON UPDATE CASCADE,
  CONSTRAINT `fk_call_sessions_caller_abonent` FOREIGN KEY (`caller_abonent_id`) REFERENCES `abonents` (`id`) ON DELETE RESTRICT ON UPDATE CASCADE,
  CONSTRAINT `fk_call_sessions_initiator_abonent` FOREIGN KEY (`initiator_abonent_id`) REFERENCES `abonents` (`id`) ON DELETE RESTRICT ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!50003 SET @saved_cs_client      = @@character_set_client */ ;
/*!50003 SET @saved_cs_results     = @@character_set_results */ ;
/*!50003 SET @saved_col_connection = @@collation_connection */ ;
/*!50003 SET character_set_client  = utf8mb4 */ ;
/*!50003 SET character_set_results = utf8mb4 */ ;
/*!50003 SET collation_connection  = utf8mb4_0900_ai_ci */ ;
/*!50003 SET @saved_sql_mode       = @@sql_mode */ ;
/*!50003 SET sql_mode              = 'NO_ENGINE_SUBSTITUTION' */ ;
DELIMITER ;;
/*!50003 CREATE*/ /*!50017 DEFINER=`u3381989_default`@`localhost`*/ /*!50003 TRIGGER `trg_call_sessions_check_not_same_party_insert` BEFORE INSERT ON `call_sessions` FOR EACH ROW BEGIN
    IF NEW.`caller_abonent_id` = NEW.`callee_abonent_id` THEN
        SIGNAL SQLSTATE '45000'
        SET MESSAGE_TEXT = 'caller_abonent_id and callee_abonent_id cannot be the same';
    END IF;
END */;;
DELIMITER ;
/*!50003 SET sql_mode              = @saved_sql_mode */ ;
/*!50003 SET character_set_client  = @saved_cs_client */ ;
/*!50003 SET character_set_results = @saved_cs_results */ ;
/*!50003 SET collation_connection  = @saved_col_connection */ ;
/*!50003 SET @saved_cs_client      = @@character_set_client */ ;
/*!50003 SET @saved_cs_results     = @@character_set_results */ ;
/*!50003 SET @saved_col_connection = @@collation_connection */ ;
/*!50003 SET character_set_client  = utf8mb4 */ ;
/*!50003 SET character_set_results = utf8mb4 */ ;
/*!50003 SET collation_connection  = utf8mb4_0900_ai_ci */ ;
/*!50003 SET @saved_sql_mode       = @@sql_mode */ ;
/*!50003 SET sql_mode              = 'NO_ENGINE_SUBSTITUTION' */ ;
DELIMITER ;;
/*!50003 CREATE*/ /*!50017 DEFINER=`u3381989_default`@`localhost`*/ /*!50003 TRIGGER `trg_call_sessions_check_not_same_party_update` BEFORE UPDATE ON `call_sessions` FOR EACH ROW BEGIN
    IF NEW.`caller_abonent_id` = NEW.`callee_abonent_id` THEN
        SIGNAL SQLSTATE '45000'
        SET MESSAGE_TEXT = 'caller_abonent_id and callee_abonent_id cannot be the same';
    END IF;
END */;;
DELIMITER ;
/*!50003 SET sql_mode              = @saved_sql_mode */ ;
/*!50003 SET character_set_client  = @saved_cs_client */ ;
/*!50003 SET character_set_results = @saved_cs_results */ ;
/*!50003 SET collation_connection  = @saved_col_connection */ ;

--
-- Table structure for table `client_fn`
--

DROP TABLE IF EXISTS `client_fn`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `client_fn` (
  `id` int NOT NULL AUTO_INCREMENT,
  `kkt_id` int NOT NULL,
  `fn_expires_at` datetime DEFAULT NULL,
  `created_at` datetime DEFAULT NULL,
  `updated_at` datetime DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_client_fn_kkt_id` (`kkt_id`),
  CONSTRAINT `fk_client_fn_kkt` FOREIGN KEY (`kkt_id`) REFERENCES `client_kkt` (`id`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=188 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `client_kkt`
--

DROP TABLE IF EXISTS `client_kkt`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `client_kkt` (
  `id` int NOT NULL AUTO_INCREMENT,
  `client_id` int DEFAULT NULL,
  `company_id` int DEFAULT NULL,
  `kkt_slot` varchar(50) NOT NULL,
  `address` text,
  `created_at` datetime DEFAULT NULL,
  `updated_at` datetime DEFAULT NULL,
  `name` varchar(255) DEFAULT NULL COMMENT 'Название торговой точки',
  `phone` varchar(50) DEFAULT NULL COMMENT 'Телефон торговой точки',
  `landmark` text COMMENT 'Ориентир / комментарий',
  `kkt_registered_at` date DEFAULT NULL COMMENT 'Дата регистрации ККТ',
  `kkt_model` varchar(100) DEFAULT NULL COMMENT 'Модель ККТ',
  `kkt_serial_number` varchar(50) DEFAULT NULL COMMENT 'ЗН ККТ',
  `kkt_reg_number` varchar(50) DEFAULT NULL COMMENT 'РН ККТ',
  `kkt_firmware` varchar(100) DEFAULT NULL COMMENT 'Прошивка ККТ',
  `fn_serial_number` varchar(50) DEFAULT NULL COMMENT 'ЗН ФН',
  `fn_type` varchar(50) DEFAULT NULL COMMENT 'Тип ФН (ФН-15, ФН-36)',
  `fn_manufacturer` varchar(100) DEFAULT NULL COMMENT 'Производитель ФН',
  `ofd_inn` varchar(20) DEFAULT NULL COMMENT 'ИНН ОФД',
  `ofd_name` varchar(100) DEFAULT NULL COMMENT 'Название ОФД',
  `trade_system` varchar(100) DEFAULT NULL COMMENT 'Система торговли (Сбис, 1С)',
  `sno` varchar(50) DEFAULT NULL COMMENT 'Система налогообложения',
  `marking_enabled` tinyint(1) DEFAULT '0' COMMENT 'Маркировка включена',
  `marking_type` varchar(100) DEFAULT NULL COMMENT 'Вид маркировки',
  `scanner_model` varchar(100) DEFAULT NULL COMMENT 'Модель сканера',
  `imei` varchar(50) DEFAULT NULL COMMENT 'IMEI устройства',
  `sim_number` varchar(50) DEFAULT NULL COMMENT 'Номер СИМ карты',
  `fn_expires_at` date DEFAULT NULL COMMENT 'Срок действия ФН',
  `ofd_expires_at` date DEFAULT NULL COMMENT 'Срок действия ОФД',
  `mark_expires_at` date DEFAULT NULL COMMENT 'Срок маркировки',
  `chz_registered` tinyint(1) DEFAULT '0' COMMENT 'Зарегистрирован в ЧЗ',
  `chz_token` varchar(255) DEFAULT NULL COMMENT 'Токен Честный Знак',
  `chz_has_ep` tinyint(1) DEFAULT '0' COMMENT 'Есть ЭП для ЧЗ',
  `chz_product_type` varchar(100) DEFAULT NULL COMMENT 'Тип продукции ЧЗ',
  `chz_fias_id` varchar(100) DEFAULT NULL COMMENT 'ФИАС ID для ЧЗ',
  `tspiot_installed_at` datetime DEFAULT NULL COMMENT 'Дата установки ТСПИоТ',
  `tspiot_tariff_expires_at` date DEFAULT NULL COMMENT 'Срок тарифа ТСПИоТ',
  `tspiot_vendor` varchar(100) DEFAULT NULL COMMENT 'Поставщик ТСПИоТ',
  `ecp_enabled` tinyint(1) DEFAULT '0' COMMENT 'ЭЦП включена',
  `ecp_type` varchar(100) DEFAULT NULL COMMENT 'Тип ЭЦП',
  `ecp_expires_at` date DEFAULT NULL COMMENT 'Срок ЭЦП',
  `ecp_comment` text COMMENT 'Комментарий к ЭЦП',
  `to_kkt_yurt` varchar(100) DEFAULT NULL COMMENT 'Привязка к ККТ Юрта',
  `has_sim` tinyint(1) DEFAULT '0' COMMENT 'Есть SIM',
  `sim_payment_at` datetime DEFAULT NULL COMMENT 'Дата оплаты SIM',
  `ofd_expires_field` varchar(100) DEFAULT NULL COMMENT 'Поле срока ОФД (доп.)',
  `evotor_utm` varchar(255) DEFAULT NULL COMMENT 'Эвотор UTM',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_client_kkt_client_slot` (`client_id`,`kkt_slot`),
  KEY `idx_client_kkt_client_id` (`client_id`),
  KEY `idx_kkt_serial` (`kkt_serial_number`),
  KEY `idx_kkt_reg_number` (`kkt_reg_number`),
  KEY `idx_fn_serial` (`fn_serial_number`),
  CONSTRAINT `fk_client_kkt_user` FOREIGN KEY (`client_id`) REFERENCES `users` (`id`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=230 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `client_mark`
--

DROP TABLE IF EXISTS `client_mark`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `client_mark` (
  `id` int NOT NULL AUTO_INCREMENT,
  `kkt_id` int NOT NULL,
  `mark_expires_at` datetime DEFAULT NULL,
  `created_at` datetime DEFAULT NULL,
  `updated_at` datetime DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_client_mark_kkt_id` (`kkt_id`),
  CONSTRAINT `fk_client_mark_kkt` FOREIGN KEY (`kkt_id`) REFERENCES `client_kkt` (`id`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=16 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `client_notes`
--

DROP TABLE IF EXISTS `client_notes`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `client_notes` (
  `id` int NOT NULL AUTO_INCREMENT,
  `client_id` int NOT NULL,
  `author_id` int NOT NULL,
  `author_name` varchar(255) NOT NULL,
  `note_type` enum('note','deal') NOT NULL DEFAULT 'note',
  `product` varchar(255) DEFAULT NULL,
  `amount` decimal(12,2) DEFAULT NULL,
  `description` text,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_client` (`client_id`)
) ENGINE=InnoDB AUTO_INCREMENT=15 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `client_ofd`
--

DROP TABLE IF EXISTS `client_ofd`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `client_ofd` (
  `id` int NOT NULL AUTO_INCREMENT,
  `kkt_id` int NOT NULL,
  `ofd_expires_at` datetime DEFAULT NULL,
  `created_at` datetime DEFAULT NULL,
  `updated_at` datetime DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_client_ofd_kkt_id` (`kkt_id`),
  CONSTRAINT `fk_client_ofd_kkt` FOREIGN KEY (`kkt_id`) REFERENCES `client_kkt` (`id`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=219 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `codes`
--

DROP TABLE IF EXISTS `codes`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `codes` (
  `id` int NOT NULL AUTO_INCREMENT,
  `code_value` varchar(255) NOT NULL,
  `is_used` tinyint(1) NOT NULL DEFAULT '0',
  `user_id` int DEFAULT NULL,
  `issued_at` datetime DEFAULT NULL,
  `code_type` enum('ofd_first','ofd_platform','ofd_other','edo','service','service_order') DEFAULT NULL,
  `operator_id` int DEFAULT NULL,
  `type_id` int DEFAULT NULL,
  `source_price` int DEFAULT NULL,
  `sell_price` int DEFAULT NULL,
  `vendor_name` varchar(255) DEFAULT NULL,
  `months_number` int DEFAULT NULL,
  `expires_at` datetime DEFAULT NULL,
  `vseofd_order_id` char(36) DEFAULT NULL,
  `kkt_factory_number` bigint DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_codes_user_id` (`user_id`),
  CONSTRAINT `fk_codes_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE SET NULL ON UPDATE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=225 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `companies`
--

DROP TABLE IF EXISTS `companies`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `companies` (
  `id` int NOT NULL AUTO_INCREMENT,
  `name` varchar(255) NOT NULL,
  `inn` varchar(20) DEFAULT NULL,
  `kpp` varchar(20) DEFAULT NULL,
  `ogrn` varchar(20) DEFAULT NULL,
  `address` text,
  `phone` varchar(50) DEFAULT NULL,
  `email` varchar(255) DEFAULT NULL,
  `website` varchar(255) DEFAULT NULL,
  `description` text,
  `note` text,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `added_by_specialist_id` int DEFAULT NULL COMMENT 'ID специалиста/директора, создавшего компанию через CRM',
  `added_by_partner_id` bigint DEFAULT NULL,
  `partner_source_type` varchar(32) DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_inn` (`inn`),
  KEY `idx_name` (`name`),
  KEY `ix_companies_added_by_partner` (`added_by_partner_id`),
  KEY `ix_companies_partner_source_type` (`partner_source_type`)
) ENGINE=InnoDB AUTO_INCREMENT=165 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `company_contacts`
--

DROP TABLE IF EXISTS `company_contacts`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `company_contacts` (
  `id` int NOT NULL AUTO_INCREMENT,
  `company_id` int NOT NULL,
  `contact_id` int NOT NULL,
  `role` varchar(100) DEFAULT NULL,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `unique_company_contact` (`company_id`,`contact_id`),
  KEY `idx_company` (`company_id`),
  KEY `idx_contact` (`contact_id`)
) ENGINE=InnoDB AUTO_INCREMENT=154 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `deadline_types`
--

DROP TABLE IF EXISTS `deadline_types`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `deadline_types` (
  `id` int NOT NULL AUTO_INCREMENT,
  `code` varchar(50) NOT NULL,
  `name` varchar(100) NOT NULL,
  `description` text,
  PRIMARY KEY (`id`),
  UNIQUE KEY `code` (`code`)
) ENGINE=InnoDB AUTO_INCREMENT=3249 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `django_admin_log`
--

DROP TABLE IF EXISTS `django_admin_log`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `django_admin_log` (
  `id` int NOT NULL AUTO_INCREMENT,
  `action_time` datetime(6) NOT NULL,
  `object_id` longtext,
  `object_repr` varchar(200) NOT NULL,
  `action_flag` smallint unsigned NOT NULL,
  `change_message` longtext NOT NULL,
  `content_type_id` int DEFAULT NULL,
  `user_id` int NOT NULL,
  PRIMARY KEY (`id`),
  KEY `django_admin_log_content_type_id_c4bce8eb_fk_django_co` (`content_type_id`),
  KEY `django_admin_log_user_id_c564eba6_fk_auth_user_id` (`user_id`),
  CONSTRAINT `django_admin_log_content_type_id_c4bce8eb_fk_django_co` FOREIGN KEY (`content_type_id`) REFERENCES `django_content_type` (`id`),
  CONSTRAINT `django_admin_log_user_id_c564eba6_fk_auth_user_id` FOREIGN KEY (`user_id`) REFERENCES `auth_user` (`id`),
  CONSTRAINT `django_admin_log_chk_1` CHECK ((`action_flag` >= 0))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `django_content_type`
--

DROP TABLE IF EXISTS `django_content_type`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `django_content_type` (
  `id` int NOT NULL AUTO_INCREMENT,
  `app_label` varchar(100) NOT NULL,
  `model` varchar(100) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `django_content_type_app_label_model_76bd3d3b_uniq` (`app_label`,`model`)
) ENGINE=InnoDB AUTO_INCREMENT=60 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `django_migrations`
--

DROP TABLE IF EXISTS `django_migrations`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `django_migrations` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `app` varchar(255) NOT NULL,
  `name` varchar(255) NOT NULL,
  `applied` datetime(6) NOT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=37 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `django_session`
--

DROP TABLE IF EXISTS `django_session`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `django_session` (
  `session_key` varchar(40) NOT NULL,
  `session_data` longtext NOT NULL,
  `expire_date` datetime(6) NOT NULL,
  PRIMARY KEY (`session_key`),
  KEY `django_session_expire_date_a5c62663` (`expire_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `equipment`
--

DROP TABLE IF EXISTS `equipment`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `equipment` (
  `id` int NOT NULL AUTO_INCREMENT,
  `name` varchar(100) NOT NULL,
  `category` enum('Кассы','Сканер','ФН','Кассовые ленты','Этикетки') DEFAULT NULL,
  `is_used` tinyint(1) NOT NULL DEFAULT '0',
  `price` int NOT NULL DEFAULT '0',
  `image_name` varchar(100) DEFAULT NULL,
  `count` int DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=12 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `ext_price_overrides`
--

DROP TABLE IF EXISTS `ext_price_overrides`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `ext_price_overrides` (
  `id` int NOT NULL AUTO_INCREMENT,
  `category` enum('ofd','service','edo') NOT NULL,
  `operator_id` int NOT NULL,
  `type_id` int NOT NULL,
  `price_fixed` int DEFAULT NULL,
  `markup_pct` decimal(8,2) DEFAULT NULL,
  `is_active` tinyint(1) NOT NULL DEFAULT '1',
  `comment` varchar(255) DEFAULT NULL,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_ext_price` (`category`,`operator_id`,`type_id`),
  KEY `idx_ext_price_active` (`category`,`is_active`)
) ENGINE=InnoDB AUTO_INCREMENT=47 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `ext_product_operators`
--

DROP TABLE IF EXISTS `ext_product_operators`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `ext_product_operators` (
  `id` int NOT NULL AUTO_INCREMENT,
  `category` enum('ofd','service','services_for_orders','edo') NOT NULL,
  `operator_id` int NOT NULL,
  `name` varchar(255) NOT NULL,
  `is_active` tinyint(1) NOT NULL DEFAULT '1',
  `sort_order` int NOT NULL DEFAULT '0',
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_ext_product_operator` (`category`,`operator_id`)
) ENGINE=InnoDB AUTO_INCREMENT=9281 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `ext_product_types`
--

DROP TABLE IF EXISTS `ext_product_types`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `ext_product_types` (
  `id` int NOT NULL AUTO_INCREMENT,
  `type_id` int NOT NULL,
  `name` varchar(255) NOT NULL,
  `months_number` int DEFAULT NULL,
  `is_active` tinyint(1) NOT NULL DEFAULT '1',
  `sort_order` int NOT NULL DEFAULT '0',
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_ext_product_type` (`type_id`)
) ENGINE=InnoDB AUTO_INCREMENT=2241 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `ext_products`
--

DROP TABLE IF EXISTS `ext_products`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `ext_products` (
  `id` int NOT NULL AUTO_INCREMENT,
  `category` enum('ofd','service','services_for_orders','edo') NOT NULL,
  `operator_id` int NOT NULL,
  `type_id` int NOT NULL,
  `external_name` varchar(255) DEFAULT NULL,
  `source_price` int DEFAULT NULL,
  `price_fixed` int DEFAULT NULL,
  `markup_pct` decimal(8,2) DEFAULT NULL,
  `is_active` tinyint(1) NOT NULL DEFAULT '1',
  `comment` varchar(255) DEFAULT NULL,
  `last_seen_at` datetime DEFAULT NULL,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_ext_product` (`category`,`operator_id`,`type_id`),
  KEY `idx_ext_products_active` (`category`,`is_active`)
) ENGINE=InnoDB AUTO_INCREMENT=1991 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `login_attempts`
--

DROP TABLE IF EXISTS `login_attempts`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `login_attempts` (
  `id` int NOT NULL AUTO_INCREMENT,
  `login` varchar(128) NOT NULL,
  `ip_address` varchar(45) NOT NULL,
  `attempted_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  `success` tinyint DEFAULT '0',
  PRIMARY KEY (`id`),
  KEY `idx_login_ip` (`login`,`ip_address`),
  KEY `idx_attempted` (`attempted_at`)
) ENGINE=InnoDB AUTO_INCREMENT=283 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `loyalty_accounts`
--

DROP TABLE IF EXISTS `loyalty_accounts`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `loyalty_accounts` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `owner_type` enum('client','partner') NOT NULL DEFAULT 'client',
  `owner_id` bigint NOT NULL COMMENT 'users.id или specialists.id',
  `is_active` tinyint(1) NOT NULL DEFAULT '1',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_loyalty_account_owner` (`owner_type`,`owner_id`),
  KEY `ix_loyalty_accounts_active` (`is_active`)
) ENGINE=InnoDB AUTO_INCREMENT=144 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `loyalty_balances`
--

DROP TABLE IF EXISTS `loyalty_balances`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `loyalty_balances` (
  `account_id` bigint NOT NULL,
  `available_amount` int NOT NULL DEFAULT '0',
  `reserved_amount` int NOT NULL DEFAULT '0',
  `total_earned` int NOT NULL DEFAULT '0' COMMENT 'Всего начислено за всё время',
  `total_spent` int NOT NULL DEFAULT '0' COMMENT 'Всего списано за всё время',
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`account_id`),
  CONSTRAINT `fk_loyalty_bal_account` FOREIGN KEY (`account_id`) REFERENCES `loyalty_accounts` (`id`) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `loyalty_transactions`
--

DROP TABLE IF EXISTS `loyalty_transactions`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `loyalty_transactions` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `account_id` bigint NOT NULL,
  `direction` enum('credit','debit') NOT NULL COMMENT 'credit=начисление, debit=списание',
  `amount` int NOT NULL COMMENT 'Всегда положительное, направление определяется direction',
  `event_type` varchar(64) NOT NULL COMMENT 'purchase, first_purchase, profile_field, referral, redeem, expiry, manual и др.',
  `idempotency_key` varchar(128) NOT NULL COMMENT 'Уникальный ключ для идемпотентности',
  `description` varchar(255) DEFAULT NULL COMMENT 'Человекочитаемое описание для отображения в боте',
  `rating_delta` int NOT NULL DEFAULT '0' COMMENT 'Изменение рейтинга пользователя',
  `reversal_of_transaction_id` bigint DEFAULT NULL COMMENT 'Ссылка на сторнируемую транзакцию',
  `expires_at` datetime DEFAULT NULL COMMENT 'Дата сгорания баллов (NULL = бессрочно)',
  `is_expired` tinyint(1) NOT NULL DEFAULT '0' COMMENT 'Флаг: баллы уже сгорели',
  `ref_entity_type` varchar(32) DEFAULT NULL COMMENT 'order, chat_payment, referral и т.д.',
  `ref_entity_id` bigint DEFAULT NULL COMMENT 'ID связанной сущности',
  `meta_json` json DEFAULT NULL COMMENT 'Произвольные данные',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_loyalty_tx_idempotency` (`idempotency_key`),
  KEY `ix_loyalty_tx_account` (`account_id`,`created_at`),
  KEY `ix_loyalty_tx_event_type` (`event_type`),
  KEY `ix_loyalty_tx_expires` (`is_expired`,`expires_at`),
  KEY `ix_loyalty_tx_reversal` (`reversal_of_transaction_id`),
  CONSTRAINT `fk_loyalty_tx_account` FOREIGN KEY (`account_id`) REFERENCES `loyalty_accounts` (`id`) ON DELETE RESTRICT,
  CONSTRAINT `fk_loyalty_tx_reversal` FOREIGN KEY (`reversal_of_transaction_id`) REFERENCES `loyalty_transactions` (`id`) ON DELETE RESTRICT
) ENGINE=InnoDB AUTO_INCREMENT=463 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `notification_delivery_log`
--

DROP TABLE IF EXISTS `notification_delivery_log`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `notification_delivery_log` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `notification_id` bigint NOT NULL,
  `channel` enum('web','telegram','email') NOT NULL,
  `recipient_id` bigint DEFAULT NULL,
  `sent_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `delivered_at` datetime DEFAULT NULL,
  `error` text,
  PRIMARY KEY (`id`),
  KEY `idx_notif` (`notification_id`),
  KEY `idx_sent` (`sent_at`),
  CONSTRAINT `fk_delivery_notif` FOREIGN KEY (`notification_id`) REFERENCES `notifications` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `notification_settings`
--

DROP TABLE IF EXISTS `notification_settings`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `notification_settings` (
  `id` int NOT NULL AUTO_INCREMENT,
  `specialist_id` bigint NOT NULL,
  `type_id` int NOT NULL,
  `enabled` tinyint(1) NOT NULL DEFAULT '1',
  `email_enabled` tinyint(1) NOT NULL DEFAULT '0',
  `telegram_enabled` tinyint(1) NOT NULL DEFAULT '1',
  `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_spec_type` (`specialist_id`,`type_id`),
  KEY `fk_settings_type` (`type_id`),
  CONSTRAINT `fk_settings_spec` FOREIGN KEY (`specialist_id`) REFERENCES `specialists` (`id`) ON DELETE CASCADE,
  CONSTRAINT `fk_settings_type` FOREIGN KEY (`type_id`) REFERENCES `notification_types` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `notification_types`
--

DROP TABLE IF EXISTS `notification_types`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `notification_types` (
  `id` int NOT NULL AUTO_INCREMENT,
  `code` varchar(50) NOT NULL,
  `name` varchar(100) NOT NULL,
  `icon` varchar(10) DEFAULT '?',
  `default_priority` enum('low','medium','high','urgent') DEFAULT 'medium',
  `description` text,
  PRIMARY KEY (`id`),
  UNIQUE KEY `code` (`code`)
) ENGINE=InnoDB AUTO_INCREMENT=4874 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `notifications`
--

DROP TABLE IF EXISTS `notifications`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `notifications` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `type_id` int NOT NULL,
  `priority` enum('low','medium','high','urgent') NOT NULL DEFAULT 'medium',
  `title` varchar(255) NOT NULL,
  `message` text,
  `client_id` int DEFAULT NULL,
  `order_id` int DEFAULT NULL,
  `chat_id` bigint DEFAULT NULL,
  `kkt_id` int DEFAULT NULL,
  `deadline_type_id` int DEFAULT NULL,
  `deadline_date` date DEFAULT NULL,
  `extra_data` json DEFAULT NULL,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `read_at` datetime DEFAULT NULL,
  `read_by` bigint DEFAULT NULL,
  `dismissed_at` datetime DEFAULT NULL,
  `dismissed_by` bigint DEFAULT NULL,
  `unique_key` varchar(255) DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `idx_unique_key` (`unique_key`),
  KEY `idx_type` (`type_id`),
  KEY `idx_priority` (`priority`),
  KEY `idx_client` (`client_id`),
  KEY `idx_order` (`order_id`),
  KEY `idx_chat` (`chat_id`),
  KEY `idx_kkt` (`kkt_id`),
  KEY `idx_deadline_date` (`deadline_date`),
  KEY `idx_created` (`created_at`),
  KEY `idx_read` (`read_at`),
  KEY `idx_unread` (`read_at`,`dismissed_at`),
  KEY `fk_notif_deadline_type` (`deadline_type_id`),
  KEY `fk_notif_read_by` (`read_by`),
  KEY `fk_notif_dismissed_by` (`dismissed_by`),
  CONSTRAINT `fk_notif_chat` FOREIGN KEY (`chat_id`) REFERENCES `support_chats` (`id`) ON DELETE CASCADE,
  CONSTRAINT `fk_notif_client` FOREIGN KEY (`client_id`) REFERENCES `users` (`id`) ON DELETE CASCADE,
  CONSTRAINT `fk_notif_deadline_type` FOREIGN KEY (`deadline_type_id`) REFERENCES `deadline_types` (`id`) ON DELETE SET NULL,
  CONSTRAINT `fk_notif_dismissed_by` FOREIGN KEY (`dismissed_by`) REFERENCES `specialists` (`id`) ON DELETE SET NULL,
  CONSTRAINT `fk_notif_kkt` FOREIGN KEY (`kkt_id`) REFERENCES `client_kkt` (`id`) ON DELETE CASCADE,
  CONSTRAINT `fk_notif_order` FOREIGN KEY (`order_id`) REFERENCES `orders` (`id`) ON DELETE CASCADE,
  CONSTRAINT `fk_notif_read_by` FOREIGN KEY (`read_by`) REFERENCES `specialists` (`id`) ON DELETE SET NULL,
  CONSTRAINT `fk_notif_type` FOREIGN KEY (`type_id`) REFERENCES `notification_types` (`id`) ON DELETE RESTRICT
) ENGINE=InnoDB AUTO_INCREMENT=33347 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `order_items`
--

DROP TABLE IF EXISTS `order_items`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `order_items` (
  `id` int NOT NULL AUTO_INCREMENT,
  `order_id` int NOT NULL,
  `code_id` int DEFAULT NULL,
  `equipment_id` int DEFAULT NULL,
  `qty` int NOT NULL DEFAULT '1',
  `price` int NOT NULL DEFAULT '0',
  `created_at` datetime DEFAULT NULL,
  `legacy_item_type` varchar(255) DEFAULT NULL,
  `legacy_item_title` varchar(255) DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_order_items_order_id` (`order_id`),
  KEY `idx_order_items_code_id` (`code_id`),
  KEY `idx_order_items_equipment_id` (`equipment_id`),
  CONSTRAINT `fk_order_items_code` FOREIGN KEY (`code_id`) REFERENCES `codes` (`id`) ON DELETE SET NULL ON UPDATE CASCADE,
  CONSTRAINT `fk_order_items_equipment` FOREIGN KEY (`equipment_id`) REFERENCES `equipment` (`id`) ON DELETE SET NULL ON UPDATE CASCADE,
  CONSTRAINT `fk_order_items_order_id` FOREIGN KEY (`order_id`) REFERENCES `orders` (`id`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=177 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `orders`
--

DROP TABLE IF EXISTS `orders`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `orders` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int DEFAULT NULL,
  `company_id` int DEFAULT NULL,
  `specialist_id` int DEFAULT NULL,
  `amount` int NOT NULL DEFAULT '0',
  `status` enum('CANCELLED','CONFIRMED','INIT','SHIPPED') DEFAULT NULL,
  `source` varchar(32) DEFAULT NULL,
  `payment_info` text,
  `description` text,
  `admin_note` text,
  `author_name` varchar(255) DEFAULT NULL,
  `payment_id` varchar(255) DEFAULT NULL,
  `created_at` datetime DEFAULT NULL,
  `updated_at` datetime DEFAULT NULL,
  `paid_at` datetime DEFAULT NULL,
  `delivery` enum('Доставка','Самовывоз','Доставка Озон/ВБ') DEFAULT NULL,
  `address_delivery` varchar(200) DEFAULT NULL,
  `address` text,
  PRIMARY KEY (`id`),
  KEY `idx_orders_user_id` (`user_id`),
  CONSTRAINT `fk_orders_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE SET NULL ON UPDATE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=144 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `partner_financial_profile`
--

DROP TABLE IF EXISTS `partner_financial_profile`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `partner_financial_profile` (
  `specialist_id` bigint NOT NULL,
  `base_revenue_pct` decimal(5,2) NOT NULL DEFAULT '50.00' COMMENT 'Базовая ставка % от выручки',
  `override_revenue_pct` decimal(5,2) DEFAULT NULL COMMENT 'Ручная ставка от директора (перекрывает base)',
  `rating` int NOT NULL DEFAULT '100' COMMENT 'Рейтинг партнёра (начальный 100)',
  `rating_tier` varchar(32) NOT NULL DEFAULT 'standard',
  `is_active` tinyint(1) NOT NULL DEFAULT '1',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`specialist_id`),
  KEY `ix_pfp_rating` (`rating`),
  KEY `ix_pfp_active` (`is_active`),
  CONSTRAINT `fk_pfp_specialist` FOREIGN KEY (`specialist_id`) REFERENCES `specialists` (`id`) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `partner_rating_events`
--

DROP TABLE IF EXISTS `partner_rating_events`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `partner_rating_events` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `specialist_id` bigint NOT NULL,
  `delta` int NOT NULL COMMENT 'Изменение рейтинга (+/-)',
  `reason` varchar(64) NOT NULL COMMENT 'complaint, bonus, monthly_review, manual',
  `comment` varchar(500) DEFAULT NULL,
  `initiated_by` bigint DEFAULT NULL COMMENT 'specialists.id директора/админа (NULL = система)',
  `rating_before` int NOT NULL,
  `rating_after` int NOT NULL,
  `ref_entity_type` varchar(32) DEFAULT NULL COMMENT 'chat, order и т.д.',
  `ref_entity_id` bigint DEFAULT NULL,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `ix_pre_specialist` (`specialist_id`,`created_at`),
  KEY `ix_pre_reason` (`reason`),
  CONSTRAINT `fk_pre_specialist` FOREIGN KEY (`specialist_id`) REFERENCES `specialists` (`id`) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `partner_salary_adjustments`
--

DROP TABLE IF EXISTS `partner_salary_adjustments`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `partner_salary_adjustments` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `salary_period_id` bigint NOT NULL,
  `type` enum('bonus','penalty','correction') NOT NULL,
  `amount_rub` decimal(12,2) NOT NULL COMMENT 'Положительная для бонуса, отрицательная для штрафа',
  `comment` varchar(500) NOT NULL,
  `created_by` bigint NOT NULL COMMENT 'specialists.id директора',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `ix_psa_period` (`salary_period_id`),
  KEY `fk_psa_created_by` (`created_by`),
  CONSTRAINT `fk_psa_created_by` FOREIGN KEY (`created_by`) REFERENCES `specialists` (`id`) ON DELETE RESTRICT,
  CONSTRAINT `fk_psa_period` FOREIGN KEY (`salary_period_id`) REFERENCES `partner_salary_periods` (`id`) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `partner_salary_periods`
--

DROP TABLE IF EXISTS `partner_salary_periods`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `partner_salary_periods` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `specialist_id` bigint NOT NULL,
  `period_month` char(7) NOT NULL COMMENT 'Формат YYYY-MM',
  `status` enum('draft','approved','paid') NOT NULL DEFAULT 'draft',
  `gross_revenue_rub` decimal(12,2) NOT NULL DEFAULT '0.00' COMMENT 'Общая выручка по партнёру за месяц',
  `effective_pct` decimal(5,2) NOT NULL DEFAULT '0.00' COMMENT 'Итоговый % (base/override + tier-бонус)',
  `base_salary_rub` decimal(12,2) NOT NULL DEFAULT '0.00' COMMENT 'gross_revenue * effective_pct / 100',
  `adjustments_rub` decimal(12,2) NOT NULL DEFAULT '0.00' COMMENT 'Сумма ручных корректировок',
  `final_salary_rub` decimal(12,2) NOT NULL DEFAULT '0.00' COMMENT 'base_salary + adjustments',
  `approved_by` bigint DEFAULT NULL,
  `approved_at` datetime DEFAULT NULL,
  `paid_at` datetime DEFAULT NULL,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_psp_specialist_month` (`specialist_id`,`period_month`),
  KEY `ix_psp_status` (`status`),
  KEY `ix_psp_period` (`period_month`),
  CONSTRAINT `fk_psp_specialist` FOREIGN KEY (`specialist_id`) REFERENCES `specialists` (`id`) ON DELETE RESTRICT
) ENGINE=InnoDB AUTO_INCREMENT=12 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `rating_tiers`
--

DROP TABLE IF EXISTS `rating_tiers`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `rating_tiers` (
  `id` int NOT NULL AUTO_INCREMENT,
  `target_type` enum('client','partner') NOT NULL,
  `tier_name` varchar(32) NOT NULL COMMENT 'bronze, silver, gold, platinum',
  `min_rating` int NOT NULL COMMENT 'Минимальный рейтинг для попадания в уровень',
  `bonus_pct` decimal(5,2) NOT NULL DEFAULT '0.00' COMMENT 'Доп. % к начислению (для клиента) или к ставке (для партнёра)',
  `description` varchar(255) DEFAULT NULL,
  `is_active` tinyint(1) NOT NULL DEFAULT '1',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_rt_type_tier` (`target_type`,`tier_name`),
  KEY `ix_rt_lookup` (`target_type`,`min_rating`)
) ENGINE=InnoDB AUTO_INCREMENT=19 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `referral_activations`
--

DROP TABLE IF EXISTS `referral_activations`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `referral_activations` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `user_id` int DEFAULT NULL,
  `referral_code_id` bigint NOT NULL,
  `activated_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `raw_text` varchar(64) DEFAULT NULL,
  `scan_source` varchar(32) DEFAULT NULL COMMENT 'qr, deep_link, manual',
  `verification_status` enum('pending','verified','rejected') DEFAULT NULL,
  `verification_at` datetime DEFAULT NULL,
  `unique_hash` varchar(64) DEFAULT NULL COMMENT 'Хэш для дедупликации QR-сканов',
  `source_platform` varchar(16) DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_user_once` (`user_id`),
  KEY `ix_ref_code` (`referral_code_id`),
  KEY `ix_ra_unique_hash` (`unique_hash`),
  CONSTRAINT `fk_activations_users` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE ON UPDATE CASCADE,
  CONSTRAINT `fk_ra_code` FOREIGN KEY (`referral_code_id`) REFERENCES `referral_codes` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=33 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `referral_codes`
--

DROP TABLE IF EXISTS `referral_codes`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `referral_codes` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `code` varchar(32) DEFAULT NULL,
  `owner_name` varchar(128) NOT NULL,
  `owner_note` varchar(255) DEFAULT NULL,
  `is_active` tinyint NOT NULL DEFAULT '1',
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `user_id` int DEFAULT NULL,
  `owner_user_id` bigint DEFAULT NULL,
  `code_type` varchar(16) DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_ref_code` (`code`),
  KEY `ix_ref_active` (`is_active`),
  KEY `fk_ref_codes_users` (`user_id`),
  CONSTRAINT `fk_ref_codes_users` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=71 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `referral_qr_collection`
--

DROP TABLE IF EXISTS `referral_qr_collection`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `referral_qr_collection` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `owner_user_id` int NOT NULL,
  `invited_user_id` int NOT NULL,
  `collected_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_owner_invited` (`owner_user_id`,`invited_user_id`),
  KEY `ix_owner` (`owner_user_id`),
  KEY `ix_invited` (`invited_user_id`),
  CONSTRAINT `fk_rqc_invited_user` FOREIGN KEY (`invited_user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE ON UPDATE CASCADE,
  CONSTRAINT `fk_rqc_owner_user` FOREIGN KEY (`owner_user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `specialist_promocodes`
--

DROP TABLE IF EXISTS `specialist_promocodes`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `specialist_promocodes` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `promo_code` varchar(64) NOT NULL,
  `specialist_id` bigint NOT NULL,
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_promo` (`promo_code`),
  KEY `ix_promo_spec` (`specialist_id`),
  CONSTRAINT `fk_promo_spec` FOREIGN KEY (`specialist_id`) REFERENCES `specialists` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `specialists`
--

DROP TABLE IF EXISTS `specialists`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `specialists` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `phone` varchar(32) DEFAULT NULL,
  `tg_username` varchar(64) DEFAULT NULL,
  `tg_user_id` bigint DEFAULT NULL,
  `name` varchar(128) NOT NULL,
  `profession` tinyint NOT NULL,
  `role` varchar(32) NOT NULL DEFAULT 'specialist',
  `is_active` tinyint NOT NULL DEFAULT '1',
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `phone_verified` tinyint NOT NULL DEFAULT '0',
  `login` varchar(50) DEFAULT NULL COMMENT 'Логин для сайта',
  `password_hash` varchar(255) DEFAULT NULL COMMENT 'Хэш пароля (SHA256+salt)',
  `is_director` tinyint(1) DEFAULT '0' COMMENT 'Флаг директора',
  `max_user_id` bigint DEFAULT NULL,
  `max_username` varchar(64) DEFAULT NULL,
  `linked_user_id` bigint DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_spec_tg_user_id` (`tg_user_id`),
  UNIQUE KEY `idx_specialists_login` (`login`),
  KEY `ix_spec_prof_active` (`profession`,`is_active`)
) ENGINE=InnoDB AUTO_INCREMENT=32 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `support_chat_declines`
--

DROP TABLE IF EXISTS `support_chat_declines`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `support_chat_declines` (
  `chat_id` bigint NOT NULL,
  `specialist_id` bigint NOT NULL,
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`chat_id`,`specialist_id`),
  KEY `ix_decl_spec` (`specialist_id`),
  CONSTRAINT `fk_decl_chat` FOREIGN KEY (`chat_id`) REFERENCES `support_chats` (`id`) ON DELETE CASCADE,
  CONSTRAINT `fk_decl_spec` FOREIGN KEY (`specialist_id`) REFERENCES `specialists` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `support_chat_files`
--

DROP TABLE IF EXISTS `support_chat_files`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `support_chat_files` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `chat_id` bigint NOT NULL,
  `message_id` bigint DEFAULT NULL,
  `sender` tinyint NOT NULL,
  `original_name` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci NOT NULL,
  `stored_name` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci NOT NULL,
  `mime` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci NOT NULL,
  `size_bytes` bigint NOT NULL,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_chat_id` (`chat_id`),
  KEY `idx_message_id` (`message_id`),
  CONSTRAINT `fk_file_chat` FOREIGN KEY (`chat_id`) REFERENCES `support_chats` (`id`) ON DELETE CASCADE,
  CONSTRAINT `fk_file_msg` FOREIGN KEY (`message_id`) REFERENCES `support_chat_messages` (`id`) ON DELETE SET NULL
) ENGINE=InnoDB AUTO_INCREMENT=29 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `support_chat_messages`
--

DROP TABLE IF EXISTS `support_chat_messages`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `support_chat_messages` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `chat_id` bigint NOT NULL,
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `sender` tinyint NOT NULL,
  `text` text NOT NULL,
  `is_read` tinyint NOT NULL DEFAULT '1',
  `edited_at` timestamp NULL DEFAULT NULL,
  `edited_by` bigint DEFAULT NULL,
  `is_edited` tinyint NOT NULL DEFAULT '0',
  PRIMARY KEY (`id`),
  KEY `ix_chat_time` (`chat_id`,`id`),
  KEY `ix_msg_edited` (`is_edited`),
  KEY `ix_msg_unread` (`chat_id`,`sender`,`is_read`),
  KEY `ix_chat_sender_id` (`chat_id`,`sender`,`id`),
  CONSTRAINT `fk_msg_chat` FOREIGN KEY (`chat_id`) REFERENCES `support_chats` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=1241 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `support_chat_offers`
--

DROP TABLE IF EXISTS `support_chat_offers`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `support_chat_offers` (
  `chat_id` bigint NOT NULL,
  `specialist_id` bigint NOT NULL,
  `tg_chat_id` bigint NOT NULL,
  `message_id` bigint NOT NULL,
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`chat_id`,`specialist_id`,`message_id`),
  KEY `ix_offer_chat` (`chat_id`),
  KEY `ix_offer_spec` (`specialist_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `support_chat_payments`
--

DROP TABLE IF EXISTS `support_chat_payments`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `support_chat_payments` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `chat_id` bigint NOT NULL,
  `specialist_id` bigint NOT NULL,
  `amount_rub` int NOT NULL,
  `payment_id` varchar(64) NOT NULL,
  `pay_url` text NOT NULL,
  `status` varchar(32) NOT NULL DEFAULT 'NEW',
  `bonus_points_applied` int NOT NULL DEFAULT '0',
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `gross_amount_rub` int DEFAULT NULL COMMENT 'Полная сумма до скидки',
  `bonus_discount_rub` int NOT NULL DEFAULT '0' COMMENT 'Скидка за счёт баллов (в рублях)',
  `cash_amount_rub` int NOT NULL DEFAULT '0',
  `bonus_reservation_key` varchar(128) DEFAULT NULL,
  `payment_method` varchar(32) NOT NULL DEFAULT 'CASH_ONLY',
  `paid_at` timestamp NULL DEFAULT NULL,
  `net_amount_rub` int DEFAULT NULL COMMENT 'Итого к оплате (gross - bonus_discount)',
  `bonus_points_spent` int NOT NULL DEFAULT '0' COMMENT 'Списано баллов',
  `bonus_rate_snapshot` decimal(8,4) DEFAULT NULL COMMENT 'Курс баллы→руб на момент оплаты',
  `salary_base_amount_rub` int DEFAULT NULL COMMENT 'База для расчёта зарплаты партнёра (= gross, не net)',
  `order_id` int DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_payment_id` (`payment_id`),
  KEY `ix_pay_chat` (`chat_id`),
  KEY `fk_pay_spec` (`specialist_id`),
  KEY `ix_pay_chat_status` (`chat_id`,`status`),
  KEY `ix_pay_paid_at` (`paid_at`),
  KEY `idx_scp_order_id` (`order_id`),
  CONSTRAINT `fk_pay_chat` FOREIGN KEY (`chat_id`) REFERENCES `support_chats` (`id`) ON DELETE CASCADE,
  CONSTRAINT `fk_pay_spec` FOREIGN KEY (`specialist_id`) REFERENCES `specialists` (`id`) ON DELETE RESTRICT
) ENGINE=InnoDB AUTO_INCREMENT=20 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `support_chat_read_state`
--

DROP TABLE IF EXISTS `support_chat_read_state`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `support_chat_read_state` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `chat_id` bigint NOT NULL,
  `specialist_id` bigint NOT NULL,
  `last_read_message_id` bigint NOT NULL DEFAULT '0',
  `last_read_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `ux_chat_reader` (`chat_id`,`specialist_id`),
  KEY `ix_reader_chat` (`specialist_id`,`chat_id`),
  KEY `ix_chat_last_read` (`chat_id`,`last_read_message_id`)
) ENGINE=InnoDB AUTO_INCREMENT=2572 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `support_chat_tokens`
--

DROP TABLE IF EXISTS `support_chat_tokens`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `support_chat_tokens` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `token` varchar(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci NOT NULL,
  `chat_id` bigint NOT NULL,
  `role` varchar(16) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci NOT NULL,
  `actor_id` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci DEFAULT NULL,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `expires_at` datetime NOT NULL,
  `revoked_at` datetime DEFAULT NULL,
  `replaced_by` bigint DEFAULT NULL,
  `last_seen_at` datetime DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `token` (`token`),
  KEY `idx_chat_tokens_chat_id` (`chat_id`),
  KEY `idx_chat_tokens_token` (`token`),
  KEY `idx_chat_tokens_expires` (`expires_at`),
  KEY `ix_support_chat_tokens_replaced_by` (`replaced_by`),
  CONSTRAINT `fk_token_chat` FOREIGN KEY (`chat_id`) REFERENCES `support_chats` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=1796 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `support_chats`
--

DROP TABLE IF EXISTS `support_chats`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `support_chats` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `client_user_id` bigint NOT NULL,
  `specialist_id` bigint DEFAULT NULL,
  `profession` int NOT NULL,
  `category` varchar(128) DEFAULT NULL,
  `request_type` varchar(128) DEFAULT NULL,
  `status` enum('pending','active','consultation','closed','test') DEFAULT NULL,
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `accepted_at` timestamp NULL DEFAULT NULL,
  `closed_at` timestamp NULL DEFAULT NULL,
  `open_key` tinyint GENERATED ALWAYS AS ((case when (`status` in (_utf8mb4'pending',_utf8mb4'active')) then 1 else NULL end)) STORED,
  `close_req_client` tinyint NOT NULL DEFAULT '0',
  `close_req_agent` tinyint NOT NULL DEFAULT '0',
  `close_req_client_at` timestamp NULL DEFAULT NULL,
  `close_req_agent_at` timestamp NULL DEFAULT NULL,
  `source` varchar(32) DEFAULT 'telegram',
  PRIMARY KEY (`id`),
  KEY `ix_spec_status` (`specialist_id`,`status`),
  KEY `ix_client_prof` (`client_user_id`,`profession`),
  CONSTRAINT `fk_chat_spec` FOREIGN KEY (`specialist_id`) REFERENCES `specialists` (`id`) ON DELETE SET NULL
) ENGINE=InnoDB AUTO_INCREMENT=276 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `system_settings`
--

DROP TABLE IF EXISTS `system_settings`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `system_settings` (
  `name` varchar(100) NOT NULL,
  `value_type` enum('number','text','json') NOT NULL,
  `char_value` varchar(1000) DEFAULT NULL,
  `number_value` decimal(12,4) DEFAULT NULL,
  `description` varchar(255) DEFAULT NULL,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `updated_by` bigint DEFAULT NULL,
  `group_name` varchar(64) DEFAULT NULL COMMENT 'Группа настройки для UI',
  `sort_order` int NOT NULL DEFAULT '0' COMMENT 'Порядок отображения в группе',
  `unit` varchar(32) DEFAULT NULL COMMENT 'Единица измерения: %, баллы, руб, дни',
  PRIMARY KEY (`name`),
  KEY `idx_system_settings_value_type` (`value_type`),
  KEY `idx_system_settings_updated_by` (`updated_by`),
  CONSTRAINT `fk_system_settings_updated_by` FOREIGN KEY (`updated_by`) REFERENCES `specialists` (`id`) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `task_clients`
--

DROP TABLE IF EXISTS `task_clients`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `task_clients` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `task_id` bigint NOT NULL,
  `client_id` int NOT NULL,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uniq_task_client` (`task_id`,`client_id`),
  KEY `idx_task_clients_client` (`client_id`),
  CONSTRAINT `fk_task_clients_client` FOREIGN KEY (`client_id`) REFERENCES `users` (`id`) ON DELETE CASCADE,
  CONSTRAINT `fk_task_clients_task` FOREIGN KEY (`task_id`) REFERENCES `tasks` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=128 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `task_history`
--

DROP TABLE IF EXISTS `task_history`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `task_history` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `task_id` bigint NOT NULL,
  `actor_id` bigint DEFAULT NULL,
  `action_type` varchar(64) NOT NULL,
  `comment` text,
  `old_status` varchar(32) DEFAULT NULL,
  `new_status` varchar(32) DEFAULT NULL,
  `reminder_at` datetime DEFAULT NULL,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_task_history_task` (`task_id`),
  KEY `idx_task_history_created` (`created_at`)
) ENGINE=InnoDB AUTO_INCREMENT=39 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `tasks`
--

DROP TABLE IF EXISTS `tasks`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `tasks` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `title` varchar(500) NOT NULL,
  `description` text,
  `source_type` varchar(32) NOT NULL DEFAULT 'manual',
  `source_id` bigint DEFAULT NULL,
  `dedupe_key` varchar(255) DEFAULT NULL,
  `meta_json` longtext,
  `director_id` bigint DEFAULT NULL,
  `created_by` bigint NOT NULL,
  `assignee_id` bigint DEFAULT NULL,
  `client_id` bigint DEFAULT NULL,
  `assigned_at` datetime DEFAULT NULL,
  `completed_at` datetime DEFAULT NULL,
  `postponed_until` datetime DEFAULT NULL,
  `deadline` datetime DEFAULT NULL,
  `priority` varchar(16) NOT NULL DEFAULT 'medium',
  `status` varchar(32) NOT NULL DEFAULT 'awaiting_assignment',
  `result_comment` text,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `idx_tasks_dedupe` (`dedupe_key`),
  KEY `idx_tasks_assignee` (`assignee_id`),
  KEY `idx_tasks_status` (`status`),
  KEY `idx_tasks_deadline` (`deadline`),
  KEY `idx_tasks_source` (`source_type`,`source_id`),
  KEY `idx_tasks_created_by` (`created_by`)
) ENGINE=InnoDB AUTO_INCREMENT=202 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `ui_state`
--

DROP TABLE IF EXISTS `ui_state`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `ui_state` (
  `tg_user_id` bigint NOT NULL,
  `main_menu_mid` bigint DEFAULT NULL,
  `boot_notice_mid` bigint DEFAULT NULL,
  `updated_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`tg_user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `user_custom_fields`
--

DROP TABLE IF EXISTS `user_custom_fields`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `user_custom_fields` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL,
  `field_name` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci NOT NULL,
  `field_value` text CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci,
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_user_field` (`user_id`,`field_name`),
  KEY `idx_user_id` (`user_id`),
  CONSTRAINT `fk_custom_fields_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=2 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `user_loyalty_profile`
--

DROP TABLE IF EXISTS `user_loyalty_profile`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `user_loyalty_profile` (
  `user_id` bigint NOT NULL COMMENT 'users.id (internal)',
  `rating` int NOT NULL DEFAULT '0',
  `tier` varchar(32) NOT NULL DEFAULT 'bronze',
  `total_orders` int NOT NULL DEFAULT '0',
  `total_spent_rub` decimal(12,2) NOT NULL DEFAULT '0.00',
  `last_activity_at` datetime DEFAULT NULL,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`user_id`),
  KEY `ix_ulp_rating` (`rating`),
  KEY `ix_ulp_tier` (`tier`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `users`
--

DROP TABLE IF EXISTS `users`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `users` (
  `id` int NOT NULL AUTO_INCREMENT,
  `tg_user_id` bigint DEFAULT NULL,
  `max_user_id` bigint DEFAULT NULL,
  `first_name` varchar(255) DEFAULT NULL,
  `last_name` varchar(255) DEFAULT NULL,
  `phone` varchar(50) DEFAULT NULL,
  `email` varchar(255) DEFAULT NULL,
  `tg_username` varchar(255) DEFAULT NULL,
  `inn` varchar(20) DEFAULT NULL,
  `org_name` varchar(255) DEFAULT NULL,
  `kpp` varchar(20) DEFAULT NULL,
  `city` varchar(255) DEFAULT NULL,
  `birth_date` date DEFAULT NULL,
  `amo_company_id` varchar(64) DEFAULT NULL,
  `consent_accepted` tinyint(1) NOT NULL DEFAULT '0',
  `consent_accepted_at` datetime DEFAULT NULL,
  `is_verified` tinyint(1) NOT NULL DEFAULT '0',
  `created_at` datetime DEFAULT NULL,
  `updated_at` datetime DEFAULT NULL,
  `referral_code_id` bigint DEFAULT NULL,
  `address` varchar(200) DEFAULT NULL,
  `kkt_factory_number` varchar(25) DEFAULT NULL,
  `we_added` tinyint(1) NOT NULL DEFAULT '0',
  `added_by_specialist_id` int DEFAULT NULL COMMENT 'ID специалиста/директора, создавшего клиента через CRM',
  `added_by_partner_id` bigint DEFAULT NULL,
  `source_partner_id` bigint DEFAULT NULL,
  `partner_source_type` varchar(32) DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_users_tg_user_id` (`tg_user_id`),
  UNIQUE KEY `max_user_id` (`max_user_id`),
  KEY `idx_users_inn` (`inn`),
  KEY `idx_users_phone` (`phone`),
  KEY `fk_users_referral_code` (`referral_code_id`),
  KEY `ix_users_added_by_partner` (`added_by_partner_id`),
  KEY `ix_users_source_partner` (`source_partner_id`),
  KEY `ix_users_partner_source_type` (`partner_source_type`),
  CONSTRAINT `fk_users_referral_code` FOREIGN KEY (`referral_code_id`) REFERENCES `referral_codes` (`id`) ON DELETE SET NULL ON UPDATE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=9392 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!50112 SET @disable_bulk_load = IF (@is_rocksdb_supported, 'SET SESSION rocksdb_bulk_load = @old_rocksdb_bulk_load', 'SET @dummy_rocksdb_bulk_load = 0') */;
/*!50112 PREPARE s FROM @disable_bulk_load */;
/*!50112 EXECUTE s */;
/*!50112 DEALLOCATE PREPARE s */;
/*!40103 SET TIME_ZONE=@OLD_TIME_ZONE */;

/*!40101 SET SQL_MODE=@OLD_SQL_MODE */;
/*!40014 SET FOREIGN_KEY_CHECKS=@OLD_FOREIGN_KEY_CHECKS */;
/*!40014 SET UNIQUE_CHECKS=@OLD_UNIQUE_CHECKS */;
/*!40101 SET CHARACTER_SET_CLIENT=@OLD_CHARACTER_SET_CLIENT */;
/*!40101 SET CHARACTER_SET_RESULTS=@OLD_CHARACTER_SET_RESULTS */;
/*!40101 SET COLLATION_CONNECTION=@OLD_COLLATION_CONNECTION */;
/*!40111 SET SQL_NOTES=@OLD_SQL_NOTES */;

-- Dump completed on 2026-05-03 22:47:23
