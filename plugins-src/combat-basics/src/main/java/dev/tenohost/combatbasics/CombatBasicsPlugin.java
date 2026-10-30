package dev.tenohost.combatbasics;

import org.bukkit.Bukkit;
import org.bukkit.Location;
import org.bukkit.Material;
import org.bukkit.NamespacedKey;
import org.bukkit.Particle;
import org.bukkit.Sound;
import org.bukkit.World;
import org.bukkit.block.Block;
import org.bukkit.block.BlockFace;
import org.bukkit.block.data.type.Leaves;
import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;
import org.bukkit.entity.AbstractArrow;
import org.bukkit.entity.Display;
import org.bukkit.entity.FallingBlock;
import org.bukkit.entity.LivingEntity;
import org.bukkit.entity.ItemDisplay;
import org.bukkit.entity.Player;
import org.bukkit.entity.SmallFireball;
import org.bukkit.event.EventHandler;
import org.bukkit.event.EventPriority;
import org.bukkit.event.Listener;
import org.bukkit.event.block.BlockBreakEvent;
import org.bukkit.event.entity.EntityDamageByEntityEvent;
import org.bukkit.event.entity.EntityShootBowEvent;
import org.bukkit.event.entity.ProjectileHitEvent;
import org.bukkit.event.inventory.InventoryClickEvent;
import org.bukkit.event.inventory.PrepareAnvilEvent;
import org.bukkit.event.player.PlayerInteractEntityEvent;
import org.bukkit.event.world.ChunkLoadEvent;
import org.bukkit.event.world.LootGenerateEvent;
import org.bukkit.inventory.ItemStack;
import org.bukkit.inventory.PlayerInventory;
import org.bukkit.inventory.meta.EnchantmentStorageMeta;
import org.bukkit.inventory.meta.ItemMeta;
import org.bukkit.persistence.PersistentDataContainer;
import org.bukkit.persistence.PersistentDataType;
import org.bukkit.plugin.java.JavaPlugin;
import org.bukkit.util.Transformation;
import org.bukkit.util.Vector;
import net.kyori.adventure.text.Component;
import net.kyori.adventure.text.format.NamedTextColor;
import net.kyori.adventure.text.format.TextDecoration;
import org.joml.Quaternionf;
import org.joml.Vector3f;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ThreadLocalRandom;

public final class CombatBasicsPlugin extends JavaPlugin implements Listener {
    private final Set<UUID> displays = new HashSet<>();
    private NamespacedKey shieldKey;
    private NamespacedKey hellRuneKey;
    private NamespacedKey hellBookKey;
    private NamespacedKey hellArrowKey;
    private NamespacedKey hellFireballKey;
    private NamespacedKey cutRuneKey;
    private NamespacedKey cutBookKey;
    private NamespacedKey vxKey;
    private NamespacedKey vyKey;
    private NamespacedKey vzKey;
    private NamespacedKey yawKey;
    private NamespacedKey pitchKey;
    private NamespacedKey rollKey;
    private NamespacedKey pickupAtKey;
    private boolean applyingSweep;

    @Override
    public void onEnable() {
        saveDefaultConfig();
        shieldKey = new NamespacedKey(this, "shield-drop");
        hellRuneKey = new NamespacedKey(this, "hell-rune");
        hellBookKey = new NamespacedKey(this, "hell-rune-book");
        hellArrowKey = new NamespacedKey(this, "hell-rune-arrow");
        hellFireballKey = new NamespacedKey(this, "hell-rune-fireball");
        cutRuneKey = new NamespacedKey(this, "cut-rune");
        cutBookKey = new NamespacedKey(this, "cut-rune-book");
        vxKey = new NamespacedKey(this, "velocity-x");
        vyKey = new NamespacedKey(this, "velocity-y");
        vzKey = new NamespacedKey(this, "velocity-z");
        yawKey = new NamespacedKey(this, "rotation-yaw");
        pitchKey = new NamespacedKey(this, "rotation-pitch");
        rollKey = new NamespacedKey(this, "rotation-roll");
        pickupAtKey = new NamespacedKey(this, "pickup-at");
        getServer().getPluginManager().registerEvents(this, this);
        for (World world : Bukkit.getWorlds()) {
            world.getEntitiesByClass(ItemDisplay.class).stream()
                    .filter(this::isShieldDisplay)
                    .map(ItemDisplay::getUniqueId)
                    .forEach(displays::add);
        }
        getServer().getScheduler().runTaskTimer(this, this::tickDisplays, 1L, 1L);
        var command = getCommand("combatbasics");
        if (command == null) {
            throw new IllegalStateException("Falta registrar /combat en plugin.yml");
        }
        command.setExecutor(this::onCommand);
        command.setTabCompleter((sender, ignored, alias, args) -> tabComplete(sender, args));
    }

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        if (!sender.hasPermission("combatbasics.give")) {
            sender.sendMessage("§cNo tienes permiso para dar esta espada.");
            return true;
        }
        if (args.length != 2 && args.length != 3
                || !args[0].equalsIgnoreCase("give")
                || args.length == 3
                && !args[2].equalsIgnoreCase("hell") && !args[2].equalsIgnoreCase("cut")) {
            sender.sendMessage("§eUsa: /combat give <jugador> [hell|cut]");
            return true;
        }
        Player target = Bukkit.getPlayerExact(args[1]);
        if (target == null) {
            sender.sendMessage("§cEse jugador no está conectado.");
            return true;
        }
        boolean cut = args.length == 3 && args[2].equalsIgnoreCase("cut");
        ItemStack book = cut ? createCutBook() : createHellBook();
        Map<Integer, ItemStack> remaining = target.getInventory().addItem(book);
        for (ItemStack item : remaining.values()) {
            target.getWorld().dropItemNaturally(target.getLocation(), item);
        }
        sender.sendMessage("§aLibro de la runa " + (cut ? "Cut" : "Hell")
                + " entregado a " + target.getName() + ".");
        return true;
    }

    private List<String> tabComplete(CommandSender sender, String[] args) {
        if (args.length == 1) {
            return matching(List.of("give"), args[0]);
        }
        if (args.length == 2 && args[0].equalsIgnoreCase("give")) {
            return matching(Bukkit.getOnlinePlayers().stream().map(Player::getName).toList(), args[1]);
        }
        if (args.length == 3 && args[0].equalsIgnoreCase("give")) {
            return matching(List.of("hell", "cut"), args[2]);
        }
        return List.of();
    }

    private List<String> matching(List<String> values, String prefix) {
        return values.stream().filter(value -> value.regionMatches(true, 0, prefix, 0, prefix.length()))
                .toList();
    }

    private ItemStack createHellBook() {
        ItemStack book = new ItemStack(Material.ENCHANTED_BOOK);
        EnchantmentStorageMeta meta = (EnchantmentStorageMeta) book.getItemMeta();
        meta.displayName(Component.text("Hell", NamedTextColor.LIGHT_PURPLE)
                .decorate(TextDecoration.BOLD).decoration(TextDecoration.ITALIC, false));
        meta.lore(List.of(
                Component.text("RUNA DE ARCO", NamedTextColor.DARK_GRAY)
                        .decorate(TextDecoration.BOLD).decoration(TextDecoration.ITALIC, false),
                Component.text("Hell", NamedTextColor.LIGHT_PURPLE)
                        .decorate(TextDecoration.BOLD).decoration(TextDecoration.ITALIC, false),
                Component.text("Aplícala a un arco en un yunque.", NamedTextColor.GRAY)
                        .decoration(TextDecoration.ITALIC, false),
                Component.text("Las flechas invocan una espiral de fuego.", NamedTextColor.GRAY)
                        .decoration(TextDecoration.ITALIC, false)));
        meta.setEnchantmentGlintOverride(true);
        meta.getPersistentDataContainer().set(hellBookKey, PersistentDataType.BYTE, (byte) 1);
        book.setItemMeta(meta);
        return book;
    }

    private ItemStack createCutBook() {
        ItemStack book = new ItemStack(Material.ENCHANTED_BOOK);
        EnchantmentStorageMeta meta = (EnchantmentStorageMeta) book.getItemMeta();
        meta.displayName(Component.text("Cut", NamedTextColor.LIGHT_PURPLE)
                .decorate(TextDecoration.BOLD).decoration(TextDecoration.ITALIC, false));
        meta.lore(List.of(
                Component.text("RUNA DE HACHA", NamedTextColor.DARK_GRAY)
                        .decorate(TextDecoration.BOLD).decoration(TextDecoration.ITALIC, false),
                Component.text("Cut", NamedTextColor.LIGHT_PURPLE)
                        .decorate(TextDecoration.BOLD).decoration(TextDecoration.ITALIC, false),
                Component.text("Aplícala a un hacha en un yunque.", NamedTextColor.GRAY)
                        .decoration(TextDecoration.ITALIC, false),
                Component.text("Agáchate y espera a que cargue el ataque.", NamedTextColor.GRAY)
                        .decoration(TextDecoration.ITALIC, false),
                Component.text("Derriba árboles enteros con gravedad.", NamedTextColor.GRAY)
                        .decoration(TextDecoration.ITALIC, false)));
        meta.setEnchantmentGlintOverride(true);
        meta.getPersistentDataContainer().set(cutBookKey, PersistentDataType.BYTE, (byte) 1);
        book.setItemMeta(meta);
        return book;
    }

    private boolean isHellBook(ItemStack item) {
        return item != null && item.getType() == Material.ENCHANTED_BOOK
                && item.getItemMeta() != null
                && item.getItemMeta().getPersistentDataContainer()
                .has(hellBookKey, PersistentDataType.BYTE);
    }

    private boolean isCutBook(ItemStack item) {
        return item != null && item.getType() == Material.ENCHANTED_BOOK
                && item.getItemMeta() != null
                && item.getItemMeta().getPersistentDataContainer()
                .has(cutBookKey, PersistentDataType.BYTE);
    }

    private boolean hasHellRune(ItemStack item) {
        return item != null && item.getType() == Material.BOW && item.getItemMeta() != null
                && item.getItemMeta().getPersistentDataContainer()
                .has(hellRuneKey, PersistentDataType.BYTE);
    }

    private boolean hasCutRune(ItemStack item) {
        return item != null && isAxe(item.getType()) && item.getItemMeta() != null
                && item.getItemMeta().getPersistentDataContainer()
                .has(cutRuneKey, PersistentDataType.BYTE);
    }

    @EventHandler(priority = EventPriority.HIGHEST)
    public void onPrepareAnvil(PrepareAnvilEvent event) {
        ItemStack target = event.getInventory().getItem(0);
        ItemStack book = event.getInventory().getItem(1);
        boolean hell = isHellBook(book);
        boolean cut = isCutBook(book);
        if (!hell && !cut) {
            return;
        }
        if (target == null
                || hell && (target.getType() != Material.BOW || hasHellRune(target))
                || cut && (!isAxe(target.getType()) || hasCutRune(target))) {
            event.setResult(null);
            return;
        }
        ItemStack result = target.clone();
        ItemMeta meta = result.getItemMeta();
        String rune = hell ? "Hell" : "Cut";
        meta.getPersistentDataContainer().set(
                hell ? hellRuneKey : cutRuneKey, PersistentDataType.BYTE, (byte) 1);
        List<Component> lore = new ArrayList<>(meta.lore() == null ? List.of() : meta.lore());
        lore.add(Component.text(rune, NamedTextColor.LIGHT_PURPLE)
                .decorate(TextDecoration.BOLD).decoration(TextDecoration.ITALIC, false));
        meta.lore(lore);
        result.setItemMeta(meta);
        event.setResult(result);
        event.getInventory().setRepairCost(1);
    }

    @EventHandler(priority = EventPriority.HIGHEST, ignoreCancelled = true)
    public void onAnvilTake(InventoryClickEvent event) {
        if (event.getRawSlot() != 2) {
            return;
        }
        ItemStack book = event.getView().getItem(1);
        boolean hell = isHellBook(book);
        boolean cut = isCutBook(book);
        if ((!hell && !cut) || event.getView().getItem(0) == null
                || hell && (event.getView().getItem(0).getType() != Material.BOW
                || !hasHellRune(event.getCurrentItem()))
                || cut && (!isAxe(event.getView().getItem(0).getType())
                || !hasCutRune(event.getCurrentItem()))) {
            event.setCancelled(true);
        }
    }

    @EventHandler(priority = EventPriority.HIGHEST, ignoreCancelled = true)
    public void onHellBowShoot(EntityShootBowEvent event) {
        if (!(event.getProjectile() instanceof AbstractArrow arrow) || !hasHellRune(event.getBow())) {
            return;
        }
        arrow.getPersistentDataContainer().set(hellArrowKey, PersistentDataType.BYTE, (byte) 1);
    }

    @EventHandler(priority = EventPriority.HIGH, ignoreCancelled = true)
    public void onHellArrowHit(ProjectileHitEvent event) {
        if (!(event.getEntity() instanceof AbstractArrow arrow)
                || !arrow.getPersistentDataContainer().has(hellArrowKey, PersistentDataType.BYTE)
                || !(event.getHitEntity() instanceof LivingEntity victim)
                || victim.isDead()) {
            return;
        }
        var shooter = arrow.getShooter();
        int count = Math.max(1, Math.min(24, getConfig().getInt("hell-fireball-count", 10)));
        double radius = Math.max(1.0, Math.min(8.0,
                getConfig().getDouble("hell-spiral-radius", 4.5)));
        long interval = Math.max(1L, getConfig().getLong("hell-fireball-interval-ticks", 2L));
        for (int index = 0; index < count; index++) {
            int step = index;
            getServer().getScheduler().runTaskLater(this, () -> {
                if (!victim.isValid() || victim.isDead()) {
                    return;
                }
                Location center = victim.getLocation().add(0, victim.getHeight() * 0.5, 0);
                double angle = step * (Math.PI * 2.0 / 5.0);
                double height = Math.sin(step * 0.65) * 1.2;
                Location launch = center.clone().add(
                        Math.cos(angle) * radius, height, Math.sin(angle) * radius);
                Vector direction = center.toVector().subtract(launch.toVector()).normalize();
                victim.getWorld().spawn(launch, SmallFireball.class, entity -> {
                    if (shooter != null) {
                        entity.setShooter(shooter);
                    }
                    entity.getPersistentDataContainer().set(
                            hellFireballKey, PersistentDataType.BYTE, (byte) 1);
                    entity.setDirection(direction);
                    entity.setIsIncendiary(false);
                    entity.setYield(0.0f);
                    entity.setVelocity(direction.multiply(getConfig().getDouble(
                            "hell-fireball-speed", 0.75)));
                });
            }, step * interval);
        }
        arrow.remove();
    }

    @EventHandler(priority = EventPriority.HIGH, ignoreCancelled = true)
    public void onHellFireballHit(ProjectileHitEvent event) {
        if (event.getEntity() instanceof SmallFireball fireball
                && fireball.getPersistentDataContainer()
                .has(hellFireballKey, PersistentDataType.BYTE)) {
            fireball.getWorld().playSound(
                    fireball.getLocation(), Sound.ENTITY_FIREWORK_ROCKET_BLAST, 1.0f, 0.0f);
        }
    }

    @EventHandler(priority = EventPriority.HIGHEST, ignoreCancelled = true)
    public void onCutSweep(EntityDamageByEntityEvent event) {
        if (applyingSweep || !(event.getDamager() instanceof Player attacker)
                || !(event.getEntity() instanceof LivingEntity primary)
                || !hasCutRune(attacker.getInventory().getItemInMainHand())
                || !isCutReady(attacker)) {
            return;
        }
        double radius = Math.max(0.5, Math.min(3.0,
                getConfig().getDouble("cut-sweep-radius", 1.0)));
        double damage = Math.max(1.0, getConfig().getDouble("cut-sweep-damage", 1.0));
        Vector facing = attacker.getLocation().getDirection().setY(0);
        if (facing.lengthSquared() < 0.0001) {
            return;
        }
        facing.normalize();
        applyingSweep = true;
        try {
            for (var entity : attacker.getWorld().getNearbyEntities(
                    attacker.getLocation(), radius, 0.75, radius)) {
                if (!(entity instanceof LivingEntity target)
                        || target.equals(attacker) || target.equals(primary) || target.isDead()) {
                    continue;
                }
                Vector toTarget = target.getLocation().toVector()
                        .subtract(attacker.getLocation().toVector()).setY(0);
                if (toTarget.lengthSquared() == 0 || facing.dot(toTarget.normalize()) <= 0) {
                    continue;
                }
                target.damage(damage, attacker);
                target.getWorld().spawnParticle(
                        Particle.SWEEP_ATTACK, target.getLocation().add(0, 1, 0),
                        1, 0.1, 0.1, 0.1, 0.0);
            }
        } finally {
            applyingSweep = false;
        }
        attacker.getWorld().playSound(
                attacker.getLocation(), Sound.ENTITY_PLAYER_ATTACK_SWEEP, 1.0f, 1.0f);
    }

    @EventHandler(priority = EventPriority.HIGHEST, ignoreCancelled = true)
    public void onCutTree(BlockBreakEvent event) {
        Player player = event.getPlayer();
        ItemStack axe = player.getInventory().getItemInMainHand();
        if (!hasCutRune(axe) || !isCutReady(player) || !isTreeLog(event.getBlock().getType())) {
            return;
        }
        List<Block> tree = findTreeBlocks(event.getBlock());
        if (tree.isEmpty()) {
            return;
        }
        event.setCancelled(true);
        event.setDropItems(false);
        Vector fallDirection = player.getLocation().getDirection().setY(0);
        if (fallDirection.lengthSquared() == 0) {
            fallDirection = new Vector(0, 0, 1);
        }
        fallDirection.normalize();
        int lowestY = tree.stream().mapToInt(Block::getY).min().orElse(event.getBlock().getY());
        List<FallingTreeBlock> falling = tree.stream()
                .map(block -> new FallingTreeBlock(
                        block.getLocation().add(0.5, 0.0, 0.5), block.getBlockData().clone(),
                        isTreeLog(block.getType())))
                .toList();
        for (Block block : tree) {
            block.setType(Material.AIR, false);
        }
        for (FallingTreeBlock part : falling) {
            double heightFactor = Math.max(0.0, part.location().getY() - lowestY) * 0.018;
            FallingBlock entity = part.location().getWorld().spawnFallingBlock(
                    part.location(), part.data());
            entity.setDropItem(part.log());
            entity.setHurtEntities(false);
            entity.setVelocity(fallDirection.clone().multiply(0.16 + heightFactor)
                    .setY(0.12 + Math.min(heightFactor, 0.3)));
        }
        player.getWorld().playSound(event.getBlock().getLocation(),
                event.getBlock().getBlockData().getSoundGroup().getBreakSound(), 1.0f, 0.65f);
    }

    private boolean isCutReady(Player player) {
        return player.isSneaking() && player.isOnGround() && !player.isSprinting()
                && player.getAttackCooldown() >= 0.9f;
    }

    private boolean isAxe(Material material) {
        return material.name().endsWith("_AXE");
    }

    private boolean isTreeLog(Material material) {
        return material.name().endsWith("_LOG") || material.name().endsWith("_STEM");
    }

    private List<Block> findTreeBlocks(Block origin) {
        int maxLogs = getConfig().getInt("cut-tree-max-logs", 256);
        int maxLeaves = getConfig().getInt("cut-tree-max-leaves", 512);
        if (maxLogs < 1 || maxLeaves < 0) {
            return List.of();
        }
        List<Block> logs = new ArrayList<>();
        Set<Long> seen = new HashSet<>();
        List<Block> pending = new ArrayList<>();
        pending.add(origin);
        seen.add(origin.getBlockKey());
        BlockFace[] neighbors = {
                BlockFace.UP, BlockFace.DOWN, BlockFace.NORTH,
                BlockFace.SOUTH, BlockFace.EAST, BlockFace.WEST
        };
        for (int cursor = 0; cursor < pending.size(); cursor++) {
            Block current = pending.get(cursor);
            logs.add(current);
            if (logs.size() > maxLogs) {
                return List.of();
            }
            for (BlockFace face : neighbors) {
                Block adjacent = current.getRelative(face);
                if (isTreeLog(adjacent.getType()) && seen.add(adjacent.getBlockKey())) {
                    pending.add(adjacent);
                }
            }
        }

        List<Block> tree = new ArrayList<>(logs);
        Set<Long> leavesSeen = new HashSet<>();
        for (Block log : logs) {
            for (int dx = -3; dx <= 3; dx++) {
                for (int dy = -3; dy <= 3; dy++) {
                    for (int dz = -3; dz <= 3; dz++) {
                        Block candidate = log.getRelative(dx, dy, dz);
                        if (!(candidate.getBlockData() instanceof Leaves leaves)
                                || leaves.isPersistent() || !leavesSeen.add(candidate.getBlockKey())) {
                            continue;
                        }
                        tree.add(candidate);
                        if (tree.size() - logs.size() > maxLeaves) {
                            return List.of();
                        }
                    }
                }
            }
        }
        return tree;
    }

    private record FallingTreeBlock(Location location, org.bukkit.block.data.BlockData data, boolean log) {}

    @EventHandler(priority = EventPriority.HIGH)
    public void onStructureLoot(LootGenerateEvent event) {
        if (!getConfig().getBoolean("hell-books-in-structures", true)) {
            return;
        }
        if (event.getLootTable() == null) {
            return;
        }
        String path = event.getLootTable().getKey().getKey();
        if (!"minecraft".equals(event.getLootTable().getKey().getNamespace())
                || !path.startsWith("chests/")
                || ThreadLocalRandom.current().nextDouble()
                >= getConfig().getDouble("hell-book-structure-chance", 0.12)) {
            return;
        }
        List<ItemStack> loot = new ArrayList<>(event.getLoot());
        loot.add(createHellBook());
        event.setLoot(loot);
    }

    @EventHandler(priority = EventPriority.HIGHEST, ignoreCancelled = true)
    public void onBlockedAttack(EntityDamageByEntityEvent event) {
        if (!getConfig().getBoolean("enabled")
                || !(event.getDamager() instanceof Player attacker)
                || !(event.getEntity() instanceof Player blocker)
                || !isHoe(attacker.getInventory().getItemInMainHand().getType())
                || !blocker.isBlocking()
                || !isFacingAttacker(blocker, attacker)) {
            return;
        }

        PlayerInventory inventory = blocker.getInventory();
        boolean offHand = inventory.getItemInOffHand().getType() == Material.SHIELD;
        ItemStack heldShield = offHand ? inventory.getItemInOffHand() : inventory.getItemInMainHand();
        if (heldShield.getType() != Material.SHIELD) {
            return;
        }

        ItemStack shield = heldShield.clone();
        Location location = blocker.getLocation().add(0, 1.0, 0);
        Vector direction = blocker.getLocation().toVector()
                .subtract(attacker.getLocation().toVector()).setY(0);
        if (direction.lengthSquared() < 0.0001) {
            direction = blocker.getLocation().getDirection().setY(0);
        }
        direction.normalize().multiply(getConfig().getDouble("launch-horizontal", 0.28));
        Vector velocity = direction.setY(getConfig().getDouble("launch-vertical", 0.34));
        ItemDisplay display = blocker.getWorld().spawn(location, ItemDisplay.class, entity -> {
            entity.setItemStack(shield);
            entity.setBillboard(Display.Billboard.FIXED);
            entity.setPersistent(true);
            entity.setGravity(false);
            entity.setInvulnerable(true);
            entity.setSilent(true);
            entity.setInterpolationDuration(0);
            entity.getPersistentDataContainer().set(shieldKey, PersistentDataType.BYTE, (byte) 1);
            set(entity.getPersistentDataContainer(), vxKey, velocity.getX());
            set(entity.getPersistentDataContainer(), vyKey, velocity.getY());
            set(entity.getPersistentDataContainer(), vzKey, velocity.getZ());
            set(entity.getPersistentDataContainer(), yawKey, Math.random() * Math.PI * 2);
            set(entity.getPersistentDataContainer(), pitchKey, Math.random() * Math.PI);
            set(entity.getPersistentDataContainer(), rollKey, 0.0);
            entity.getPersistentDataContainer().set(pickupAtKey, PersistentDataType.LONG,
                    getServer().getCurrentTick() + getConfig().getLong("pickup-delay-ticks", 10));
        });

        if (offHand) {
            inventory.setItemInOffHand(null);
        } else {
            inventory.setItemInMainHand(null);
        }
        displays.add(display.getUniqueId());
        blocker.sendMessage("§e¡Tu escudo fue desarmado!");
    }

    @EventHandler
    public void onPickupClick(PlayerInteractEntityEvent event) {
        if (!(event.getRightClicked() instanceof ItemDisplay display) || !isShieldDisplay(display)) {
            return;
        }
        event.setCancelled(true);
        pickup(event.getPlayer(), display);
    }

    @EventHandler
    public void onChunkLoad(ChunkLoadEvent event) {
        for (var entity : event.getChunk().getEntities()) {
            if (entity instanceof ItemDisplay display && isShieldDisplay(display)) {
                displays.add(display.getUniqueId());
            }
        }
    }

    private void tickDisplays() {
        for (UUID id : Set.copyOf(displays)) {
            if (!(Bukkit.getEntity(id) instanceof ItemDisplay display) || !display.isValid()) {
                displays.remove(id);
                continue;
            }
            if (!isShieldDisplay(display)) {
                displays.remove(id);
                continue;
            }
            tickDisplay(display);
        }
    }

    private void tickDisplay(ItemDisplay display) {
        Location current = display.getLocation();
        World world = display.getWorld();
        Vector velocity = new Vector(
                get(display.getPersistentDataContainer(), vxKey),
                get(display.getPersistentDataContainer(), vyKey),
                get(display.getPersistentDataContainer(), vzKey));
        double gravity = getConfig().getDouble("gravity-per-tick", 0.04);
        velocity.setY(velocity.getY() - gravity);

        Location horizontal = current.clone().add(velocity.getX(), 0, velocity.getZ());
        if (!world.isChunkLoaded(horizontal.getBlockX() >> 4, horizontal.getBlockZ() >> 4)) {
            return;
        }
        if (isSolid(world, horizontal)) {
            velocity.setX(-velocity.getX() * 0.45);
            velocity.setZ(-velocity.getZ() * 0.45);
        } else {
            current.setX(horizontal.getX());
            current.setZ(horizontal.getZ());
        }

        Location vertical = current.clone().add(0, velocity.getY(), 0);
        if (!world.isChunkLoaded(vertical.getBlockX() >> 4, vertical.getBlockZ() >> 4)) {
            return;
        }
        if (isSolid(world, vertical)
                || velocity.getY() < 0 && isSolid(world, vertical.clone().add(0, -0.35, 0))) {
            double damping = getConfig().getDouble("bounce-damping", 0.55);
            velocity.setY(velocity.getY() < 0 ? Math.abs(velocity.getY()) * damping
                    : -velocity.getY() * damping);
            if (Math.abs(velocity.getY()) < 0.08) {
                velocity.setY(0);
            }
        } else {
            current.setY(vertical.getY());
        }

        if (velocity.getY() == 0) {
            velocity.setX(velocity.getX() * 0.9);
            velocity.setZ(velocity.getZ() * 0.9);
        }
        display.teleport(current);
        set(display.getPersistentDataContainer(), vxKey, velocity.getX());
        set(display.getPersistentDataContainer(), vyKey, velocity.getY());
        set(display.getPersistentDataContainer(), vzKey, velocity.getZ());

        PersistentDataContainer data = display.getPersistentDataContainer();
        double yaw = get(data, yawKey) + 0.13;
        double pitch = get(data, pitchKey) + 0.07;
        double roll = get(data, rollKey) + 0.11;
        set(data, yawKey, yaw);
        set(data, pitchKey, pitch);
        set(data, rollKey, roll);
        display.setTransformation(new Transformation(
                new Vector3f(), new Quaternionf().rotateXYZ((float) pitch, (float) yaw, (float) roll),
                new Vector3f(0.75f), new Quaternionf()));

        long pickupAt = data.getOrDefault(pickupAtKey, PersistentDataType.LONG, 0L);
        if (getServer().getCurrentTick() >= pickupAt) {
            for (var entity : world.getNearbyEntities(current, 0.8, 0.8, 0.8)) {
                if (entity instanceof Player player && pickup(player, display)) {
                    break;
                }
            }
        }
    }

    private boolean pickup(Player player, ItemDisplay display) {
        if (!display.isValid() || !isShieldDisplay(display)
                || getServer().getCurrentTick() < display.getPersistentDataContainer()
                .getOrDefault(pickupAtKey, PersistentDataType.LONG, 0L)) {
            return false;
        }
        ItemStack item = display.getItemStack().clone();
        Map<Integer, ItemStack> remaining = player.getInventory().addItem(item);
        if (!remaining.isEmpty()) {
            return false;
        }
        display.remove();
        displays.remove(display.getUniqueId());
        player.playSound(player.getLocation(), org.bukkit.Sound.ENTITY_ITEM_PICKUP, 0.5f, 1.2f);
        return true;
    }

    private boolean isFacingAttacker(Player blocker, Player attacker) {
        Vector facing = blocker.getLocation().getDirection().setY(0);
        Vector toAttacker = attacker.getLocation().toVector()
                .subtract(blocker.getLocation().toVector()).setY(0);
        if (facing.lengthSquared() == 0 || toAttacker.lengthSquared() == 0) {
            return false;
        }
        return facing.normalize().dot(toAttacker.normalize()) >= 0.0;
    }

    private boolean isSolid(World world, Location location) {
        Block block = world.getBlockAt(location);
        return !block.isPassable();
    }

    private boolean isHoe(Material material) {
        return material.name().endsWith("_HOE");
    }

    private boolean isShieldDisplay(ItemDisplay display) {
        return display.getPersistentDataContainer().has(shieldKey, PersistentDataType.BYTE);
    }

    private void set(PersistentDataContainer data, NamespacedKey key, double value) {
        data.set(key, PersistentDataType.DOUBLE, value);
    }

    private double get(PersistentDataContainer data, NamespacedKey key) {
        return data.getOrDefault(key, PersistentDataType.DOUBLE, 0.0);
    }
}
